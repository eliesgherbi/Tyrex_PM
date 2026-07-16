"""R5 end-to-end shadow lifecycle scenarios (deterministic, no network)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from tyrex_pm.core.commands import (
    ExecutionPolicy,
    SubmitOrderCommand,
    new_client_order_id,
    new_command_id,
)
from tyrex_pm.core.ids import StrategyId, new_correlation_id, new_order_id
from tyrex_pm.core.intents import IntentId, OrderSide
from tyrex_pm.execution.order_store import OrderStatus
from tyrex_pm.lifecycle.trade_lifecycle import LifecycleState
from tyrex_pm.planning.plan import PlanId
from helpers_r5 import MARKET, T0, YES, make_book, submit_buy, wired_shadow


def test_entry_full_fill_exit_flat() -> None:
    _, orders, _, portfolio, life, oms = wired_shadow()
    oms.on_book_updated(make_book(YES))
    oid = new_order_id()
    life.note_entry_submitted(oid, YES, when=T0)
    oms.submit(submit_buy(qty="8"), order_id=oid)
    assert life.state is LifecycleState.ACTIVE
    assert portfolio.net_quantity(YES) == Decimal("8")

    sell = SubmitOrderCommand(
        command_id=new_command_id(),
        plan_id=PlanId("exit"),
        intent_id=IntentId("exit"),
        strategy_id=StrategyId("reference_momentum"),
        instrument_id=YES,
        market_id=MARKET,
        side=OrderSide.SELL,
        quantity=Decimal("8"),
        limit_price=Decimal("0.48"),
        client_order_id=new_client_order_id(),
        created_at=T0 + timedelta(seconds=1),
        correlation_id=new_correlation_id(),
        causation_id=None,
        execution_policy=ExecutionPolicy.NORMAL,
    )
    eid = new_order_id()
    life.note_exit_submitted(eid, when=T0 + timedelta(seconds=1))
    oms.submit(sell, order_id=eid)
    assert portfolio.is_flat()
    assert life.state is LifecycleState.FLAT
    assert orders.get(eid).status is OrderStatus.FILLED  # type: ignore[union-attr]


def test_partial_entry_cancel_residual_active() -> None:
    _, orders, _, portfolio, life, oms = wired_shadow(cancel_residual=True)
    oms.on_book_updated(make_book(YES, asks=[("0.50", "3"), ("0.60", "100")]))
    oid = new_order_id()
    life.note_entry_submitted(oid, YES, when=T0)
    oms.submit(submit_buy(qty="10", limit="0.50"), order_id=oid)
    assert orders.get(oid).status is OrderStatus.CANCELED  # type: ignore[union-attr]
    assert portfolio.net_quantity(YES) == Decimal("3")
    assert life.state is LifecycleState.ACTIVE


def test_working_entry_later_fill() -> None:
    _, _, _, portfolio, life, oms = wired_shadow()
    oms.on_book_updated(make_book(YES, asks=[("0.60", "100")]))
    oid = new_order_id()
    life.note_entry_submitted(oid, YES, when=T0)
    oms.submit(submit_buy(limit="0.52"), order_id=oid)
    assert life.state is LifecycleState.ENTRY_PENDING
    oms.on_book_updated(
        make_book(YES, asks=[("0.50", "100")], ts=T0 + timedelta(seconds=5))
    )
    assert portfolio.net_quantity(YES) == Decimal("10")
    assert life.state is LifecycleState.ACTIVE


def test_duplicate_fill_leaves_portfolio_unchanged() -> None:
    disp, orders, ledger, portfolio, _, oms = wired_shadow()
    oms.on_book_updated(make_book(YES))
    oid = new_order_id()
    oms.submit(submit_buy(qty="4"), order_id=oid)
    fill = ledger.all_fills()[0]
    from tyrex_pm.core.execution_events import OrderFilled
    from tyrex_pm.core.events import EventSource
    from tyrex_pm.core.ids import new_event_id

    before = portfolio.net_quantity(YES)
    disp.publish(
        OrderFilled(
            event_id=new_event_id(),
            correlation_id=orders.get(oid).correlation_id,  # type: ignore[union-attr]
            causation_id=None,
            ts_event=T0,
            ts_received=T0,
            source=EventSource.SYSTEM,
            execution_id=fill.execution_id,
            order_id=oid,
            instrument_id=YES,
            side=OrderSide.BUY,
            fill_quantity=fill.quantity,
            fill_price=fill.price,
            fee_amount=fill.fee_amount,
            fee_currency=fill.fee_currency,
            cumulative_filled=fill.quantity,
            remaining_quantity=Decimal("0"),
        )
    )
    assert portfolio.net_quantity(YES) == before
    assert len(ledger.all_fills()) == 1
