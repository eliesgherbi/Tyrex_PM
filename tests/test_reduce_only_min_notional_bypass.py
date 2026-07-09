"""Reduce-only min-notional bypass unit tests."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import EnterIntent, ExitIntent, RiskContext, WalletPosition
from tyrex_pm.risk.engine import evaluate_intent
from tyrex_pm.risk.reduce_only_notional import (
    ALLOWED_REDUCE_ONLY_CONTEXTS,
    evaluate_reduce_only_min_notional_bypass,
    normalize_reduce_only_context,
)
from tyrex_pm.runtime.config import parse_app_config

TOKEN = TokenId("1234567890")


def _ctx(*, qty: Decimal = Decimal("5"), positions: tuple = ()) -> RiskContext:
    if not positions and qty > 0:
        positions = (WalletPosition(token_id=TOKEN, qty=qty, avg_price_usd=Decimal("0.5")),)
    return RiskContext(
        execution_mode=ExecutionMode.SHADOW,
        wallet_positions=positions,
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


def _app(**risk_over):
    risk = {
        "notional": {"min_usd": "1", "max_usd": "100", "max_policy": "cap"},
        "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
        "capital": {"enabled": False},
        "inventory": {"sell_requires_venue_position": True},
        "venue_min_size": {"enabled": False},
        "concurrency": {"max_orders_in_flight": 10},
        "readiness": {"require_wallet_sync": False},
    }
    risk.update(risk_over)
    return parse_app_config(
        risk=risk,
        strategy={"kind": "paired_binary", "enabled": True, "paired_binary": {"owner_id": "pb", "market_id": "m1", "yes_token_id": str(TOKEN), "no_token_id": "n2"}},
        runtime={
            "execution_mode": "shadow",
            "reporting": {"enabled": False},
            "market_data": {"enabled": True, "max_book_age_s": 5},
            "execution": {"planner": {"enabled": True}},
        },
    )


def test_buy_below_min_notional_still_blocked() -> None:
    intent = EnterIntent(
        token_id=TOKEN,
        side=Side.BUY,
        size=Decimal("1"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    d = evaluate_intent(intent, _ctx(), app=_app(), run_id=RunId("r"))
    assert not d.approved
    assert "notional_below_min" in d.reason_codes


def test_normal_sell_below_min_blocked_without_context() -> None:
    intent = ExitIntent(
        token_id=TOKEN,
        side=Side.SELL,
        size=Decimal("1"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.FAK,
    )
    d = evaluate_intent(intent, _ctx(qty=Decimal("1")), app=_app(), run_id=RunId("r"))
    assert not d.approved
    assert "notional_below_min" in d.reason_codes


def test_urgent_exit_sell_below_min_allowed() -> None:
    intent = ExitIntent(
        token_id=TOKEN,
        side=Side.SELL,
        size=Decimal("1.79"),
        limit_price=Decimal("0.42"),
        order_style=OrderStyle.FAK,
        urgency="urgent",
    )
    ext = {"paired_binary_reason": "urgent_exit", "paired_binary_sizing": {"owner_allocation": "1.79"}}
    d = evaluate_intent(
        intent,
        _ctx(qty=Decimal("1.79")),
        app=_app(),
        run_id=RunId("r"),
        reduce_only_context="urgent_exit",
        intent_extensions=ext,
    )
    assert d.approved, d.reason_codes
    assert d.extensions is not None
    assert d.extensions.get("reduce_only_min_notional_bypass") is True


def test_entry_fill_timeout_context_normalized() -> None:
    assert normalize_reduce_only_context("entry_fill_timeout") == "entry_fill_timeout"
    assert normalize_reduce_only_context("asymmetric_entry_timeout") == "saga_abort_unwind"


def test_all_allowed_contexts() -> None:
    for ctx in ALLOWED_REDUCE_ONLY_CONTEXTS:
        intent = ExitIntent(
            token_id=TOKEN,
            side=Side.SELL,
            size=Decimal("1"),
            limit_price=Decimal("0.5"),
            order_style=OrderStyle.FAK,
            urgency="urgent",
        )
        r = evaluate_reduce_only_min_notional_bypass(
            intent,
            _ctx(qty=Decimal("1")),
            min_usd=Decimal("1"),
            reduce_only_context=ctx,
            intent_extensions={"paired_binary_reason": ctx},
        )
        assert r.allowed, ctx


def test_sell_denied_when_qty_exceeds_owned() -> None:
    intent = ExitIntent(
        token_id=TOKEN,
        side=Side.SELL,
        size=Decimal("10"),
        limit_price=Decimal("0.05"),
        order_style=OrderStyle.FAK,
        urgency="urgent",
    )
    r = evaluate_reduce_only_min_notional_bypass(
        intent,
        _ctx(qty=Decimal("1.79")),
        min_usd=Decimal("1"),
        reduce_only_context="entry_fill_timeout",
    )
    assert not r.allowed
    assert r.deny_reason == "size_exceeds_available"


def test_sell_denied_without_position_evidence() -> None:
    intent = ExitIntent(
        token_id=TOKEN,
        side=Side.SELL,
        size=Decimal("1"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.FAK,
        urgency="urgent",
    )
    r = evaluate_reduce_only_min_notional_bypass(
        intent,
        _ctx(qty=Decimal("0"), positions=()),
        min_usd=Decimal("1"),
        reduce_only_context="urgent_exit",
    )
    assert not r.allowed
    assert r.deny_reason == "missing_position_evidence"
