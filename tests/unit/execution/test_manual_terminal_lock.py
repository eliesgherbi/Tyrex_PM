"""Reducer invariants for terminal manual sessions."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.execution.evidence import (
    ExecutionRole,
    ManualInterventionRequired,
    OrderSide,
    TradeStatus,
    TradeStatusObserved,
)
from tyrex_pm.execution.orders import MarketSellOrderSpec, TimeInForce
from tyrex_pm.execution.reducer import reduce_execution_event
from tyrex_pm.execution.session_state import (
    ExecutionPhase,
    ExecutionSessionState,
    OrderExecutionState,
    SessionIdentity,
)


def _state_with_open_position() -> ExecutionSessionState:
    state = ExecutionSessionState()
    state.identity = SessionIdentity(
        session_id="s1",
        strategy_id="ask70",
        market_id="m1",
        window_id="w1",
        token_id="t1",
    )
    state.phase = ExecutionPhase.POSITION_OPEN
    state.confirmed_position_shares = Decimal("14.96875")
    spec = MarketSellOrderSpec(
        order_id="exit-1",
        market_id="m1",
        instrument_id="t1",
        token_id="t1",
        shares=Decimal("14.96"),
        minimum_price=Decimal("0.01"),
        time_in_force=TimeInForce.FAK,
    )
    order = OrderExecutionState(spec=spec, role=ExecutionRole.EXIT)
    order.venue_order_id = "venue-exit-1"
    state.orders[spec.order_id] = order
    state.exit_order_ids = (spec.order_id,)
    return state


def test_manual_intervention_stays_terminal_after_late_trade() -> None:
    state = _state_with_open_position()
    now = datetime.now(timezone.utc)
    reduce_execution_event(
        state,
        ManualInterventionRequired(
            event_id="manual-1",
            session_id="s1",
            observed_at=now,
            observed_monotonic_ns=1,
            dedupe_key="manual:exit_retry_budget_exhausted",
            reason="exit_retry_budget_exhausted",
        ),
    )
    assert state.phase is ExecutionPhase.MANUAL_INTERVENTION
    assert state.terminal

    reduce_execution_event(
        state,
        TradeStatusObserved(
            event_id="trade-1",
            session_id="s1",
            observed_at=now,
            observed_monotonic_ns=2,
            dedupe_key="trade-1",
            venue_trade_id="vt-1",
            venue_order_id="venue-exit-1",
            order_id="exit-1",
            token_id="t1",
            side=OrderSide.SELL,
            shares=Decimal("14.96"),
            price=Decimal("0.16"),
            status=TradeStatus.CONFIRMED,
            source="user_stream",
        ),
    )
    assert state.phase is ExecutionPhase.MANUAL_INTERVENTION
    assert state.terminal
    assert state.confirmed_position_shares == Decimal("0.00875")
