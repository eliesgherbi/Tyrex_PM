"""G5 resting take-profit: rest below TP, FAK at/above TP, cancel then FAK on SL."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.execution.evidence import ExecutionRole, TradeStatus
from tyrex_pm.execution.exit_sizing import remaining_exit_shares, venue_legal_sell_shares
from tyrex_pm.execution.orders import LimitOrderSpec, TimeInForce
from tyrex_pm.execution.planner import ExecutionRiskPolicy, IntentOrderPlanner
from tyrex_pm.execution.session_state import (
    ExecutionPhase,
    ExecutionSessionState,
    OrderExecutionState,
    SessionIdentity,
    TradeRecord,
)
from tyrex_pm.protection.reasons import ProtectionReason
from tyrex_pm.protection.spec import protection_spec_from_mapping
from tyrex_pm.protection.triggers import evaluate_triggers, resolve_thresholds


def _resting_spec():
    return protection_spec_from_mapping(
        {
            "mark": "bid",
            "size_mode": "full",
            "take_profit": {"style": "resting_gtc", "absolute": "0.85"},
            "stop_loss": {"style": "stop_market_fak", "absolute": "0.55"},
            "trailing": {"enabled": False, "activation": "immediate", "mark": "bid"},
        }
    )


def test_planner_builds_post_only_resting_sell() -> None:
    planner = IntentOrderPlanner(ExecutionRiskPolicy(maximum_total_debit=Decimal("5")))
    spec = planner.resting_take_profit(
        market_id="m",
        instrument_id="m:YES",
        token_id="tok",
        shares=Decimal("7"),
        limit_price=Decimal("0.85"),
    )
    assert isinstance(spec, LimitOrderSpec)
    assert spec.side.value == "SELL"
    assert spec.time_in_force is TimeInForce.GTC
    assert spec.metadata["post_only"] is True
    assert spec.shares == Decimal("7")


def test_bid_below_tp_does_not_fire_reactive_when_skipping() -> None:
    spec = _resting_spec()
    thresholds = resolve_thresholds(spec=spec, entry_price=Decimal("0.70"))
    decision = evaluate_triggers(
        spec=spec,
        mark=Decimal("0.70"),
        entry_price=Decimal("0.70"),
        peak=Decimal("0.70"),
        trailing_active=False,
        thresholds=thresholds,
        skip_take_profit=True,
    )
    assert not decision.fired


def test_bid_at_or_above_tp_fires_reactive_fak() -> None:
    spec = _resting_spec()
    thresholds = resolve_thresholds(spec=spec, entry_price=Decimal("0.70"))
    decision = evaluate_triggers(
        spec=spec,
        mark=Decimal("0.86"),
        entry_price=Decimal("0.70"),
        peak=Decimal("0.70"),
        trailing_active=False,
        thresholds=thresholds,
        skip_take_profit=False,
    )
    assert decision.fired
    assert decision.reason is ProtectionReason.PROTECTION_TP


def test_stop_loss_still_outranks_resting_tp() -> None:
    spec = _resting_spec()
    thresholds = resolve_thresholds(spec=spec, entry_price=Decimal("0.70"))
    decision = evaluate_triggers(
        spec=spec,
        mark=Decimal("0.55"),
        entry_price=Decimal("0.70"),
        peak=Decimal("0.70"),
        trailing_active=False,
        thresholds=thresholds,
        skip_take_profit=True,
    )
    assert decision.fired
    assert decision.reason is ProtectionReason.PROTECTION_SL
    assert decision.evidence["layer"] == "stop_loss"


def test_partial_resting_fill_leaves_g1_remaining() -> None:
    state = ExecutionSessionState()
    state.identity = SessionIdentity(
        session_id="s1",
        strategy_id="ask70",
        market_id="m",
        window_id="w",
        token_id="tok",
    )
    state.phase = ExecutionPhase.POSITION_OPEN
    state.confirmed_position_shares = Decimal("4")
    state.sellable_shares = Decimal("4")
    state.trades["buy"] = TradeRecord(
        venue_trade_id="buy",
        venue_order_id="entry",
        local_order_id="entry-1",
        token_id="tok",
        side="BUY",
        shares=Decimal("7"),
        price=Decimal("0.70"),
        status=TradeStatus.CONFIRMED,
        position_applied=True,
    )
    state.orders["exit-1"] = OrderExecutionState(
        role=ExecutionRole.EXIT,
        spec=IntentOrderPlanner(ExecutionRiskPolicy(maximum_total_debit=Decimal("5"))).resting_take_profit(
            market_id="m",
            instrument_id="m:YES",
            token_id="tok",
            shares=Decimal("7"),
            limit_price=Decimal("0.85"),
        ),
        venue_order_id="gtc-1",
        accepted=True,
        venue_status="live",
        cumulative_matched_hwm=Decimal("3"),
    )
    state.exit_order_ids.append("exit-1")
    remaining = remaining_exit_shares(state)
    legal = venue_legal_sell_shares(remaining, Decimal("0.01"))
    assert remaining == Decimal("4")
    assert legal == Decimal("4.00")
