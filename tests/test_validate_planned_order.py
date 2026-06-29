"""Phase 3 (architecture_enhance): validate_planned_order final-gate tests."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import ClientOrderId, RunId, TokenId
from tyrex_pm.core.models import (
    EnterIntent,
    ExitIntent,
    RiskContext,
    URGENCY_NORMAL,
    URGENCY_URGENT,
    WalletPosition,
)
from tyrex_pm.execution.models import ExecutionPlan
from tyrex_pm.risk.planned_order import validate_planned_order
from tyrex_pm.runtime.config import parse_app_config

TOKEN = TokenId("1234567890")
CID = ClientOrderId("cid-fixed-1")


def _app(**risk_overrides) -> object:
    risk = {
        "notional": {"min_usd": "0.01", "max_usd": "100", "max_policy": "deny"},
        "deployment": {"token_cap_usd": "500", "portfolio_cap_usd": "5000"},
        "venue_min_size": {"enabled": False},
        "capital": {"enabled": False, "max_wallet_age_s": 120},
        "concurrency": {"max_orders_in_flight": 8},
        "readiness": {
            "require_wallet_sync": False,
            "max_wallet_age_s_live": 120,
            "require_heartbeat_live": False,
            "require_user_ws_live": False,
        },
        "inventory": {"sell_requires_venue_position": True},
        "kill_switch": {"enabled": False},
    }
    for k, v in risk_overrides.items():
        risk[k] = v
    return parse_app_config(
        risk=risk,
        strategy={"kind": "simple_signal_test", "token_id": str(TOKEN)},
        runtime={
            "execution_mode": "shadow",
            "shadow_bootstrap": {"usdc_balance": "1", "usdc_allowance": "1"},
        },
    )


def _ctx(*, positions=(), balance="1000") -> RiskContext:
    return RiskContext(
        execution_mode=ExecutionMode.SHADOW,
        wallet_positions=tuple(positions),
        open_orders=(),
        usdc_balance=Decimal(balance),
        usdc_allowance=Decimal(balance),
        last_wallet_sync_ts=datetime.now(timezone.utc),
        mark_prices={TOKEN: Decimal("0.5")},
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
        reconcile_drift=False,
    )


def _plan(intent, *, urgency=URGENCY_NORMAL, ref_price=None) -> ExecutionPlan:
    return ExecutionPlan(
        intent=intent,
        client_order_id=CID,
        run_id=RunId("r1"),
        planner_reason=rc.PLANNER_NORMAL_ENTRY,
        urgency=urgency,
        reference_limit_price=ref_price if ref_price is not None else intent.limit_price,
        book_evidence={},
    )


def _buy(size="10", price="0.5", style=OrderStyle.GTC) -> EnterIntent:
    return EnterIntent(
        token_id=TOKEN, side=Side.BUY, size=Decimal(size), limit_price=Decimal(price), order_style=style
    )


def _sell(size="10", price="0.5", style=OrderStyle.GTC, urgency=URGENCY_NORMAL) -> ExitIntent:
    return ExitIntent(
        token_id=TOKEN,
        side=Side.SELL,
        size=Decimal(size),
        limit_price=Decimal(price),
        order_style=style,
        urgency=urgency,
    )


def test_validate_planned_order_approves_normal_buy() -> None:
    d = validate_planned_order(_plan(_buy()), _ctx(), app=_app())
    assert d.approved
    assert d.approved_intent is not None


def test_validate_planned_order_rechecks_notional() -> None:
    # 300 * 0.5 = 150 > max 100 (policy deny).
    d = validate_planned_order(_plan(_buy(size="300")), _ctx(), app=_app())
    assert not d.approved
    assert rc.NOTIONAL_ABOVE_MAX in d.reason_codes


def test_validate_planned_order_notional_cap_violation_is_planner_bug() -> None:
    app = _app(notional={"min_usd": "0.01", "max_usd": "100", "max_policy": "cap"})
    d = validate_planned_order(_plan(_buy(size="300")), _ctx(), app=app)
    assert not d.approved
    assert rc.PLANNER_NOTIONAL_VIOLATION in d.reason_codes


def test_validate_planned_order_rechecks_deployment_caps() -> None:
    app = _app(deployment={"token_cap_usd": "5", "portfolio_cap_usd": "5000"})
    # 10 * 0.5 = 5 notional... push it over token cap with a bigger order under notional max.
    d = validate_planned_order(_plan(_buy(size="40", price="0.5")), _ctx(), app=app)
    assert not d.approved
    assert rc.TOKEN_DEPLOYMENT_CAP in d.reason_codes


def test_validate_planned_order_rechecks_capital_for_buy() -> None:
    app = _app(capital={"enabled": True, "max_wallet_age_s": 120})
    d = validate_planned_order(_plan(_buy(size="100", price="0.5")), _ctx(balance="1"), app=app)
    assert not d.approved
    assert rc.INSUFFICIENT_CAPITAL in d.reason_codes


def test_validate_planned_order_rechecks_inventory_for_sell() -> None:
    # No position → naked sell.
    d = validate_planned_order(_plan(_sell()), _ctx(), app=_app())
    assert not d.approved
    assert rc.NAKED_SELL in d.reason_codes


def test_validate_planned_order_approves_sell_with_position() -> None:
    pos = WalletPosition(token_id=TOKEN, qty=Decimal("100"), avg_price_usd=Decimal("0.5"))
    d = validate_planned_order(_plan(_sell()), _ctx(positions=(pos,)), app=_app())
    assert d.approved


def test_validate_planned_order_rechecks_venue_min_size() -> None:
    app = _app(venue_min_size={"enabled": True, "default_min_size": "5", "policy": "deny"})
    d = validate_planned_order(_plan(_buy(size="2", price="0.5")), _ctx(), app=app)
    assert not d.approved
    assert rc.BELOW_VENUE_MIN_SIZE in d.reason_codes


def test_validate_planned_order_does_not_mint_new_client_order_id() -> None:
    d = validate_planned_order(_plan(_buy()), _ctx(), app=_app())
    assert d.approved
    assert d.approved_intent.client_order_id == CID


def test_validate_planned_order_denies_worsened_price() -> None:
    # planner pushed BUY price above the pre-check reference → worsened.
    plan = _plan(_buy(price="0.7"), ref_price=Decimal("0.5"))
    d = validate_planned_order(plan, _ctx(), app=_app())
    assert not d.approved
    assert rc.PLANNER_PRICE_WORSENED in d.reason_codes


def test_validate_planned_order_urgent_exit_exempt_from_price_guard() -> None:
    pos = WalletPosition(token_id=TOKEN, qty=Decimal("100"), avg_price_usd=Decimal("0.5"))
    # Marketable urgent SELL priced below reference is allowed (that's the point).
    plan = _plan(
        _sell(price="0.3", style=OrderStyle.FAK, urgency=URGENCY_URGENT),
        urgency=URGENCY_URGENT,
        ref_price=Decimal("0.5"),
    )
    d = validate_planned_order(plan, _ctx(positions=(pos,)), app=_app())
    assert d.approved


def test_validate_planned_order_emits_risk_decision_phase_planned() -> None:
    d = validate_planned_order(_plan(_buy()), _ctx(), app=_app())
    assert d.extensions is not None
    assert d.extensions["phase"] == "planned"
    assert d.extensions["planner_reason"] == rc.PLANNER_NORMAL_ENTRY
