"""Tests for the venue minimum-size policy.

Live evidence (``var/reporting/runs/live_test_reservation_life_cycle``) showed exactly one
``oms_reject`` after the in-flight reservation patch shipped: a guru signal of $272 was
clipped by ``notional_max_usd=4`` to ``4 / 0.88 ≈ 4.54`` shares, which is below
Polymarket's hard 5-share floor. The venue rejected with::

    Size (4.54) lower than the minimum: 5

This module's tests cover:

1. policy=deny short-circuits the gate with reason ``below_venue_min_size``
2. policy=bump raises ``size`` to the configured floor
3. policy=bump that would breach a higher-priority cap denies instead with bump_unsafe
4. ``risk_decision.extensions`` carry the audit fields needed for post-mortem
5. policy=deny via the full :func:`evaluate_intent` path produces no submit-ready
   ``ApprovedIntent``
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import EnterIntent, RiskContext
from tyrex_pm.risk.engine import evaluate_intent
from tyrex_pm.risk.venue_min_size import evaluate_venue_min_size
from tyrex_pm.runtime.config import (
    AppConfig,
    CapitalConfig,
    ConcurrencyConfig,
    DeploymentConfig,
    InventoryConfig,
    KillSwitchConfig,
    NotionalConfig,
    ReadinessConfig,
    ReportingConfig,
    RiskConfig,
    RuntimeConfig,
    StrategyConfig,
    VenueMinSizeConfig,
)


TOKEN = TokenId("tok-A")


def _intent(size: Decimal, price: Decimal = Decimal("0.5")) -> EnterIntent:
    return EnterIntent(
        token_id=TOKEN,
        side=Side.BUY,
        size=size,
        limit_price=price,
        order_style=OrderStyle.GTC,
    )


def _vms(policy: str = "deny", floor: Decimal = Decimal("5"), enabled: bool = True) -> VenueMinSizeConfig:
    return VenueMinSizeConfig(enabled=enabled, policy=policy, default_min_size=floor)


def _ctx(*, balance: Decimal = Decimal("100"), allowance: Decimal = Decimal("100")) -> RiskContext:
    return RiskContext(
        execution_mode=ExecutionMode.LIVE,
        wallet_positions=(),
        open_orders=(),
        usdc_balance=balance,
        usdc_allowance=allowance,
        last_wallet_sync_ts=None,
        mark_prices={},
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
        reconcile_drift=False,
        venue_truth_stale=False,
        in_flight_buy_reservations=(),
    )


def _app(vms: VenueMinSizeConfig, *, token_cap: str = "100", portfolio_cap: str = "100") -> AppConfig:
    """Build a minimal AppConfig that lets ``evaluate_intent`` run end-to-end.

    All other risk gates default to permissive values; the test then varies only the gate
    under test (notional cap or deployment cap or capital).
    """
    risk = RiskConfig(
        notional=NotionalConfig(
            min_usd=Decimal("0.01"), max_usd=Decimal("100"), max_policy="cap"
        ),
        deployment=DeploymentConfig(
            token_cap_usd=Decimal(token_cap), portfolio_cap_usd=Decimal(portfolio_cap)
        ),
        capital=CapitalConfig(enabled=True, max_wallet_age_s=120),
        inventory=InventoryConfig(sell_requires_venue_position=True),
        kill_switch=KillSwitchConfig(enabled=False),
        concurrency=ConcurrencyConfig(max_orders_in_flight=8),
        readiness=ReadinessConfig(
            require_wallet_sync=False,
            max_wallet_age_s_live=60,
            require_heartbeat_live=False,
            require_user_ws_live=False,
        ),
        venue_min_size=vms,
    )
    runtime = RuntimeConfig(
        execution_mode=ExecutionMode.LIVE,
        reporting=ReportingConfig(enabled=False, runs_dir="var/reporting/runs"),
        reconcile_interval_s=30,
        submit_grace_s=15.0,
        provisional_unknown_terminal_timeout_s=60.0,
        venue_confirm_provisional_timeout_s=60.0,
        adoption_grace_s=5.0,
        log_level="INFO",
        shadow_bootstrap=None,
    )
    # Strategy block is unused by evaluate_intent but the dataclass requires it.
    strategy = StrategyConfig.__new__(StrategyConfig)
    object.__setattr__(strategy, "guru", None)
    object.__setattr__(strategy, "filters", None)
    object.__setattr__(strategy, "sizing", None)
    object.__setattr__(strategy, "exits", None)
    return AppConfig(strategy=strategy, risk=risk, runtime=runtime, raw={})


# -----------------------------------------------------------------------------
# Pure helper tests (evaluate_venue_min_size)
# -----------------------------------------------------------------------------


def test_above_floor_passes_unchanged():
    intent = _intent(Decimal("10"))
    res = evaluate_venue_min_size(intent, _vms())
    assert res.ok is True
    assert res.intent is intent
    assert res.evidence["venue_min_size_outcome"] == "above_floor"
    assert res.evidence["venue_min_size_final_size"] == "10"


def test_disabled_gate_passes_through():
    intent = _intent(Decimal("0.1"))
    res = evaluate_venue_min_size(intent, _vms(enabled=False))
    assert res.ok is True
    assert res.intent is intent
    assert res.evidence["venue_min_size_skipped"] == "disabled"


def test_deny_policy_below_floor_returns_reason_code():
    intent = _intent(Decimal("4.54"), Decimal("0.88"))
    res = evaluate_venue_min_size(intent, _vms(policy="deny"))
    assert res.ok is False
    assert res.deny_reason == rc.BELOW_VENUE_MIN_SIZE
    ev = res.evidence
    assert ev["venue_min_size_outcome"] == "deny"
    assert ev["venue_min_size_final_size"] == "4.54"
    assert ev["venue_min_size"] == "5"
    assert ev["venue_min_size_token_id"] == str(TOKEN)
    assert ev["venue_min_size_limit_price"] == "0.88"
    assert ev["venue_min_size_final_notional_usd"] == "3.995200"


def test_bump_policy_raises_size_to_floor():
    intent = _intent(Decimal("4.54"), Decimal("0.88"))
    res = evaluate_venue_min_size(intent, _vms(policy="bump"))
    assert res.ok is True
    assert res.deny_reason is None
    assert res.intent.size == Decimal("5")
    assert res.intent.token_id == TOKEN
    ev = res.evidence
    assert ev["venue_min_size_outcome"] == "bumped"
    assert ev["venue_min_size_original_size"] == "4.54"
    assert ev["venue_min_size_bumped_size"] == "5"
    assert ev["venue_min_size_bumped_notional_usd"] == "4.400000"


# -----------------------------------------------------------------------------
# evaluate_intent (full risk engine) integration tests
# -----------------------------------------------------------------------------


def test_engine_deny_path_emits_below_venue_min_size_and_no_approved_intent():
    """The clip of $272 → $4 cap → 4.54 shares case from live evidence."""
    intent = EnterIntent(
        token_id=TOKEN,
        side=Side.BUY,
        size=Decimal("309.09"),  # raw guru size
        limit_price=Decimal("0.88"),
        order_style=OrderStyle.GTC,
    )
    app = _app(_vms(policy="deny"))
    # Tighten notional cap so the guard actually clips to 4.54 shares.
    app = replace(
        app,
        risk=replace(
            app.risk,
            notional=NotionalConfig(
                min_usd=Decimal("0.01"), max_usd=Decimal("4"), max_policy="cap"
            ),
        ),
    )
    decision = evaluate_intent(intent, _ctx(), app=app, run_id=RunId("run-1"))
    assert decision.approved is False
    assert decision.approved_intent is None
    assert rc.BELOW_VENUE_MIN_SIZE in decision.reason_codes
    ev = decision.extensions or {}
    assert ev.get("venue_min_size_outcome") == "deny"
    # Final size after clip is 4 / 0.88 ≈ 4.5454...
    assert Decimal(ev["venue_min_size_final_size"]) < Decimal("5")


def test_engine_bump_policy_succeeds_when_caps_have_headroom():
    intent = EnterIntent(
        token_id=TOKEN,
        side=Side.BUY,
        size=Decimal("309.09"),
        limit_price=Decimal("0.88"),
        order_style=OrderStyle.GTC,
    )
    app = _app(_vms(policy="bump"), token_cap="100", portfolio_cap="100")
    app = replace(
        app,
        risk=replace(
            app.risk,
            notional=NotionalConfig(
                min_usd=Decimal("0.01"), max_usd=Decimal("4"), max_policy="cap"
            ),
        ),
    )
    decision = evaluate_intent(intent, _ctx(), app=app, run_id=RunId("run-2"))
    assert decision.approved is True, decision.reason_codes
    assert decision.approved_intent is not None
    # Bumped to 5 shares despite the $4 notional cap.
    assert decision.approved_intent.intent.size == Decimal("5")
    ev = decision.extensions or {}
    assert ev.get("venue_min_size_outcome") == "bumped"
    assert ev.get("venue_min_size_bumped_size") == "5"


def test_engine_bump_unsafe_when_bump_breaches_token_cap_denies_with_evidence():
    intent = EnterIntent(
        token_id=TOKEN,
        side=Side.BUY,
        size=Decimal("309.09"),
        limit_price=Decimal("0.88"),
        order_style=OrderStyle.GTC,
    )
    # Notional cap clips to 4 shares (3.52 USD). Bumping to 5 shares = 4.40 USD pushes
    # past the per-token deployment cap of 4 USD → bump must deny.
    app = _app(_vms(policy="bump"), token_cap="4", portfolio_cap="100")
    app = replace(
        app,
        risk=replace(
            app.risk,
            notional=NotionalConfig(
                min_usd=Decimal("0.01"), max_usd=Decimal("4"), max_policy="cap"
            ),
        ),
    )
    decision = evaluate_intent(intent, _ctx(), app=app, run_id=RunId("run-3"))
    assert decision.approved is False
    assert decision.approved_intent is None
    assert rc.BELOW_VENUE_MIN_SIZE in decision.reason_codes
    ev = decision.extensions or {}
    assert ev.get("venue_min_size_outcome") == "bumped"
    assert ev.get("venue_min_size_bump_unsafe") is True
    assert ev.get("venue_min_size_bump_unsafe_reason") in (
        rc.TOKEN_DEPLOYMENT_CAP,
        rc.PORTFOLIO_DEPLOYMENT_CAP,
    )


def test_engine_bump_unsafe_when_bump_exceeds_capital_denies_with_evidence():
    intent = EnterIntent(
        token_id=TOKEN,
        side=Side.BUY,
        size=Decimal("309.09"),
        limit_price=Decimal("0.88"),
        order_style=OrderStyle.GTC,
    )
    # Plenty of cap headroom but only $4.20 of free balance — bumping to 5 shares
    # at $0.88 = $4.40 needed > $4.20 available.
    app = _app(_vms(policy="bump"), token_cap="100", portfolio_cap="100")
    app = replace(
        app,
        risk=replace(
            app.risk,
            notional=NotionalConfig(
                min_usd=Decimal("0.01"), max_usd=Decimal("4"), max_policy="cap"
            ),
        ),
    )
    decision = evaluate_intent(
        intent,
        _ctx(balance=Decimal("4.20"), allowance=Decimal("100")),
        app=app,
        run_id=RunId("run-4"),
    )
    assert decision.approved is False
    assert decision.approved_intent is None
    assert rc.BELOW_VENUE_MIN_SIZE in decision.reason_codes
    ev = decision.extensions or {}
    assert ev.get("venue_min_size_outcome") == "bumped"
    assert ev.get("venue_min_size_bump_unsafe") is True
    assert ev.get("venue_min_size_bump_unsafe_reason") in (
        rc.INSUFFICIENT_CAPITAL,
        rc.INSUFFICIENT_ALLOWANCE,
    )


def test_engine_above_floor_includes_audit_fields_on_approve():
    intent = _intent(Decimal("10"), Decimal("0.50"))
    app = _app(_vms(policy="deny"))
    decision = evaluate_intent(intent, _ctx(), app=app, run_id=RunId("run-5"))
    assert decision.approved is True
    ev = decision.extensions or {}
    assert ev.get("venue_min_size_check") is True
    assert ev.get("venue_min_size_outcome") == "above_floor"
    assert ev.get("venue_min_size_final_size") == "10"
    assert ev.get("venue_min_size") == "5"
