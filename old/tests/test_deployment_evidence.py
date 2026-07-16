"""Deployment evaluator emits structured evidence so deny reasons are diagnosable.

Before this change, ``check_deployment_caps`` returned only ``(ok, reason_code)``. When a
``portfolio_deployment_cap`` or ``deployment_mark_unknown`` deny fired, the JSONL run
contained no record of *which* tokens were deployed, *which* token lacked a mark, or
*how much* portfolio notional was estimated. Operators had to re-derive everything by
correlating intent / order / reconcile facts.

These tests exercise ``evaluate_deployment_caps`` directly: it must always return an
evidence dict whether the decision approved or denied, and the dict must localize the
failing token whenever the deny reason is token-scoped.
"""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import EnterIntent, OpenOrderView, RiskContext, WalletPosition
from tyrex_pm.risk.deployment import RiskConfigCaps, evaluate_deployment_caps


def _ctx(
    *,
    positions: tuple[WalletPosition, ...] = (),
    open_orders: tuple[OpenOrderView, ...] = (),
    marks: dict[TokenId, Decimal] | None = None,
) -> RiskContext:
    return RiskContext(
        execution_mode=ExecutionMode.SHADOW,
        wallet_positions=positions,
        open_orders=open_orders,
        usdc_balance=Decimal("1000"),
        usdc_allowance=Decimal("1000"),
        last_wallet_sync_ts=None,
        mark_prices=dict(marks or {}),
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
    )


def _intent(token: TokenId, *, size: Decimal, price: Decimal) -> EnterIntent:
    return EnterIntent(
        token_id=token,
        side=Side.BUY,
        size=size,
        limit_price=price,
        order_style=OrderStyle.GTC,
    )


def test_approve_emits_full_evidence() -> None:
    t = TokenId("alpha")
    ctx = _ctx(marks={t: Decimal("0.5")})
    caps = RiskConfigCaps(token_cap_usd=Decimal("100"), portfolio_cap_usd=Decimal("1000"))
    ok, reason, ev = evaluate_deployment_caps(
        caps, ctx, pending_intent=_intent(t, size=Decimal("10"), price=Decimal("0.5"))
    )
    assert ok and reason is None
    # USD evidence is quantized to 6 decimals for stable grep/diff (see risk.evidence_format).
    assert ev["per_token_deployed_usd"] == {"alpha": "5.000000"}
    assert ev["portfolio_deployed_usd"] == "5.000000"
    assert ev["synthetic_buy_added"] is True
    assert ev["token_cap_usd"] == "100.000000"
    assert ev["portfolio_cap_usd"] == "1000.000000"


def test_token_cap_deny_localizes_failing_token() -> None:
    """The over-budget token must be named in the evidence so operators can act on it."""
    t = TokenId("over")
    pos = WalletPosition(token_id=t, qty=Decimal("100"), avg_price_usd=Decimal("1"))
    ctx = _ctx(positions=(pos,), marks={t: Decimal("1")})
    caps = RiskConfigCaps(token_cap_usd=Decimal("50"), portfolio_cap_usd=Decimal("1000"))
    ok, reason, ev = evaluate_deployment_caps(caps, ctx)
    assert not ok
    assert reason == rc.TOKEN_DEPLOYMENT_CAP
    assert ev["denied_token_id"] == "over"
    assert ev["per_token_deployed_usd"]["over"] == "100.000000"


def test_portfolio_cap_deny_includes_total() -> None:
    """Portfolio-level deny must surface the computed total, not just the cap."""
    t1 = TokenId("a")
    t2 = TokenId("b")
    positions = (
        WalletPosition(token_id=t1, qty=Decimal("4"), avg_price_usd=Decimal("1")),
        WalletPosition(token_id=t2, qty=Decimal("4"), avg_price_usd=Decimal("1")),
    )
    ctx = _ctx(positions=positions, marks={t1: Decimal("1"), t2: Decimal("1")})
    caps = RiskConfigCaps(token_cap_usd=Decimal("100"), portfolio_cap_usd=Decimal("5"))
    ok, reason, ev = evaluate_deployment_caps(caps, ctx)
    assert not ok
    assert reason == rc.PORTFOLIO_DEPLOYMENT_CAP
    assert ev["portfolio_deployed_usd"] == "8.000000"
    assert set(ev["per_token_deployed_usd"]) == {"a", "b"}


def test_mark_unknown_for_existing_position_names_token() -> None:
    """The ghost-short pathology: a position with no mark must be pinpointed in evidence."""
    bad = TokenId("ghost")
    pos = WalletPosition(token_id=bad, qty=Decimal("3"), avg_price_usd=None)
    ctx = _ctx(positions=(pos,))
    caps = RiskConfigCaps(token_cap_usd=Decimal("100"), portfolio_cap_usd=Decimal("1000"))
    ok, reason, ev = evaluate_deployment_caps(caps, ctx)
    assert not ok
    assert reason == rc.DEPLOYMENT_MARK_UNKNOWN
    assert ev["mark_unknown_token_id"] == "ghost"
    assert "ghost" in ev["marks_missing"]


def test_mark_unknown_for_pending_intent_short_circuits() -> None:
    """An unpriceable BUY intent (no limit_price, no mark) denies before token-loop runs."""
    t = TokenId("unpriceable")
    ctx = _ctx()
    caps = RiskConfigCaps(token_cap_usd=Decimal("100"), portfolio_cap_usd=Decimal("1000"))

    intent = EnterIntent(
        token_id=t,
        side=Side.BUY,
        size=Decimal("5"),
        limit_price=None,
        order_style=OrderStyle.GTC,
    )
    ok, reason, ev = evaluate_deployment_caps(caps, ctx, pending_intent=intent)
    assert not ok
    assert reason == rc.DEPLOYMENT_MARK_UNKNOWN
    assert ev["mark_unknown_for_pending_intent"] is True


def test_engine_propagates_deployment_evidence_to_decision_extensions() -> None:
    """End-to-end: a portfolio_deployment_cap deny in the engine surfaces evidence in extensions."""
    from tyrex_pm.core.ids import RunId
    from tyrex_pm.risk.engine import evaluate_intent
    from tyrex_pm.runtime.config import parse_app_config

    t = TokenId("e2e")
    pos = WalletPosition(token_id=t, qty=Decimal("10"), avg_price_usd=Decimal("1"))
    ctx = _ctx(positions=(pos,), marks={t: Decimal("1")})

    app = parse_app_config(
        risk={
            "notional": {"min_usd": "1", "max_usd": "4", "max_policy": "cap"},
            "deployment": {"token_cap_usd": "100", "portfolio_cap_usd": "5"},
            "capital": {"enabled": False, "max_wallet_age_s": 120},
            "inventory": {"sell_requires_venue_position": True},
            "kill_switch": {"enabled": False},
            "concurrency": {"max_orders_in_flight": 8},
            "readiness": {
                "require_wallet_sync": False,
                "max_wallet_age_s_live": 60,
                "require_heartbeat_live": False,
                "require_user_ws_live": False,
            },
        },
        strategy={
            "guru": {"wallet": "0x0", "data_api_poll_interval_s": 5},
            "filters": {},
            "sizing": {},
            "exits": {},
        },
        runtime={"execution_mode": "shadow", "supervisors": {}},
    )

    intent = _intent(t, size=Decimal("4"), price=Decimal("1"))
    decision = evaluate_intent(intent, ctx, app=app, run_id=RunId("run-evidence"))

    assert not decision.approved
    assert decision.reason_codes[0] == rc.PORTFOLIO_DEPLOYMENT_CAP
    ext = decision.extensions or {}
    assert ext.get("portfolio_deployed_usd") == "14.000000"  # 10 (existing) + 4 (synthetic BUY)
    assert ext.get("per_token_deployed_usd", {}).get("e2e") == "14.000000"
    assert ext.get("token_cap_usd") == "100.000000"
    assert ext.get("portfolio_cap_usd") == "5.000000"
