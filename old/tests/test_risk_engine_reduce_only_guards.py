"""Risk engine reduce-only bypass guard tests."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import EnterIntent, ExitIntent, RiskContext, WalletPosition
from tyrex_pm.risk.engine import evaluate_intent
from tyrex_pm.risk.planned_order import validate_planned_order
from tyrex_pm.risk.reduce_only_notional import build_bypass_denied_fact
from tyrex_pm.execution.models import ExecutionPlan
from tyrex_pm.core.models import ApprovedIntent
from tyrex_pm.runtime.config import parse_app_config

TOKEN = TokenId("999")


def _ctx(qty: Decimal = Decimal("5")) -> RiskContext:
    return RiskContext(
        execution_mode=ExecutionMode.SHADOW,
        wallet_positions=(WalletPosition(token_id=TOKEN, qty=qty, avg_price_usd=Decimal("0.5")),),
        open_orders=(),
        usdc_balance=Decimal("1000"),
        usdc_allowance=Decimal("1000"),
        last_wallet_sync_ts=datetime.now(timezone.utc),
        mark_prices={TOKEN: Decimal("0.5")},
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
        reconcile_drift=False,
        venue_truth_stale=False,
    )


def _app():
    return parse_app_config(
        risk={
            "notional": {"min_usd": "1", "max_usd": "100", "max_policy": "cap"},
            "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
            "capital": {"enabled": False},
            "inventory": {"sell_requires_venue_position": True},
            "venue_min_size": {"enabled": False},
            "concurrency": {"max_orders_in_flight": 10},
            "readiness": {"require_wallet_sync": False},
        },
        strategy={"kind": "paired_binary", "enabled": True, "paired_binary": {"owner_id": "pb", "market_id": "m1", "yes_token_id": str(TOKEN), "no_token_id": "n2"}},
        runtime={
            "execution_mode": "shadow",
            "reporting": {"enabled": False},
            "market_data": {"enabled": True, "max_book_age_s": 5},
            "execution": {"planner": {"enabled": True}},
        },
    )


@pytest.mark.parametrize(
    "context",
    ["entry_fill_timeout", "saga_abort_unwind", "urgent_exit", "manual_flatten"],
)
def test_emergency_contexts_allow_small_sell(context: str) -> None:
    intent = ExitIntent(
        token_id=TOKEN,
        side=Side.SELL,
        size=Decimal("1"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.FAK,
        urgency="urgent",
    )
    d = evaluate_intent(
        intent,
        _ctx(qty=Decimal("5")),
        app=_app(),
        run_id=RunId("r"),
        reduce_only_context=context,
        intent_extensions={"paired_binary_reason": context},
    )
    assert d.approved


def test_take_profit_sell_below_min_still_blocked() -> None:
    intent = ExitIntent(
        token_id=TOKEN,
        side=Side.SELL,
        size=Decimal("1"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.FAK,
        urgency="normal",
    )
    d = evaluate_intent(
        intent,
        _ctx(qty=Decimal("5")),
        app=_app(),
        run_id=RunId("r"),
        intent_extensions={"paired_binary_reason": "take_profit"},
    )
    assert not d.approved
    assert "notional_below_min" in d.reason_codes


def test_bypass_denied_fact_shape() -> None:
    intent = ExitIntent(
        token_id=TOKEN,
        side=Side.SELL,
        size=Decimal("10"),
        limit_price=Decimal("0.42"),
        order_style=OrderStyle.FAK,
        urgency="urgent",
    )
    fact = build_bypass_denied_fact(
        intent,
        min_usd=Decimal("1"),
        deny_reason="size_exceeds_available",
        reduce_only_context="urgent_exit",
    )
    assert fact["reason"] == "size_exceeds_available"
    assert fact["context"] == "urgent_exit"


def test_planned_order_bypass_for_manual_flatten() -> None:
    app = _app()
    from tyrex_pm.core.ids import ClientOrderId

    intent = ExitIntent(
        token_id=TOKEN,
        side=Side.SELL,
        size=Decimal("1"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.FAK,
        urgency="urgent",
    )
    plan = ExecutionPlan(
        intent=intent,
        client_order_id=ClientOrderId("cid"),
        run_id=RunId("r"),
        planner_reason="planner_urgent_exit_fak",
        urgency="urgent",
    )
    d = validate_planned_order(
        plan,
        _ctx(qty=Decimal("5")),
        app=app,
        reduce_only_context="manual_flatten",
        intent_extensions={"paired_binary_reason": "manual_flatten"},
    )
    assert d.approved
    assert d.extensions is not None
    assert d.extensions.get("reduce_only_min_notional_bypass") is True
