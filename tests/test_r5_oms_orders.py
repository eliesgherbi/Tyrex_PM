"""R5 ShadowOMS + OrderStore behavior."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tyrex_pm.core.commands import CancelOrderCommand, new_command_id
from tyrex_pm.core.execution_events import ExecutionId, OrderFilled
from tyrex_pm.core.events import EventSource
from tyrex_pm.core.ids import new_event_id, new_order_id
from tyrex_pm.core.intents import OrderSide
from tyrex_pm.execution.order_store import OrderStatus, OrderTransitionError
from helpers_r5 import T0, YES, make_book, submit_buy, wired_shadow


def test_marketable_full_fill() -> None:
    _, orders, ledger, portfolio, life, oms = wired_shadow()
    oms.on_book_updated(make_book(YES))
    oid = new_order_id()
    life.note_entry_submitted(oid, YES, when=T0)
    oms.submit(submit_buy(qty="10", limit="0.52"), order_id=oid)
    rec = orders.get(oid)
    assert rec is not None
    assert rec.status is OrderStatus.FILLED
    assert rec.filled_quantity == Decimal("10")
    assert portfolio.net_quantity(YES) == Decimal("10")
    assert life.state.value == "ACTIVE"
    assert len(ledger.all_fills()) == 1


def test_non_marketable_working() -> None:
    _, orders, _, portfolio, life, oms = wired_shadow()
    oms.on_book_updated(make_book(YES, asks=[("0.60", "100")]))
    oid = new_order_id()
    life.note_entry_submitted(oid, YES, when=T0)
    oms.submit(submit_buy(qty="10", limit="0.52"), order_id=oid)
    rec = orders.get(oid)
    assert rec is not None
    assert rec.status is OrderStatus.ACCEPTED
    assert portfolio.net_quantity(YES) == 0
    assert life.state.value == "ENTRY_PENDING"


def test_multi_level_partial_then_residual_cancel() -> None:
    _, orders, _, portfolio, life, oms = wired_shadow(cancel_residual=True)
    oms.on_book_updated(
        make_book(YES, asks=[("0.50", "4"), ("0.51", "3"), ("0.55", "100")])
    )
    oid = new_order_id()
    life.note_entry_submitted(oid, YES, when=T0)
    oms.submit(submit_buy(qty="10", limit="0.51"), order_id=oid)
    rec = orders.get(oid)
    assert rec is not None
    assert rec.filled_quantity == Decimal("7")
    assert rec.status is OrderStatus.CANCELED
    assert portfolio.net_quantity(YES) == Decimal("7")
    assert life.state.value == "ACTIVE"


def test_cancel_before_fill_returns_flat() -> None:
    _, orders, _, portfolio, life, oms = wired_shadow()
    oms.on_book_updated(make_book(YES, asks=[("0.60", "100")]))
    oid = new_order_id()
    life.note_entry_submitted(oid, YES, when=T0)
    oms.submit(submit_buy(limit="0.52"), order_id=oid)
    oms.cancel(
        CancelOrderCommand(
            command_id=new_command_id(),
            order_id=oid,
            reason_code="USER_CANCEL",
            created_at=T0,
            correlation_id=orders.get(oid).correlation_id,  # type: ignore[union-attr]
            causation_id=None,
        )
    )
    assert orders.get(oid).status is OrderStatus.CANCELED  # type: ignore[union-attr]
    assert portfolio.net_quantity(YES) == 0
    assert life.state.value == "FLAT"


def test_repeated_snapshot_no_double_fill() -> None:
    _, orders, ledger, portfolio, _, oms = wired_shadow()
    book = make_book(YES)
    oms.on_book_updated(book)
    oid = new_order_id()
    oms.submit(submit_buy(qty="5"), order_id=oid)
    assert portfolio.net_quantity(YES) == Decimal("5")
    fills_n = len(ledger.all_fills())
    oms.on_book_updated(book)
    oms.on_book_updated(book)
    assert len(ledger.all_fills()) == fills_n
    assert portfolio.net_quantity(YES) == Decimal("5")
    assert orders.get(oid).status is OrderStatus.FILLED  # type: ignore[union-attr]


def test_reconnect_invalidation_blocks_fills() -> None:
    _, orders, _, portfolio, life, oms = wired_shadow()
    oms.invalidate_books()
    oid = new_order_id()
    life.note_entry_submitted(oid, YES, when=T0)
    oms.submit(submit_buy(limit="0.52"), order_id=oid)
    assert orders.get(oid).status is OrderStatus.ACCEPTED  # type: ignore[union-attr]
    assert portfolio.net_quantity(YES) == 0
    oms.on_book_updated(make_book(YES, asks=[("0.50", "100")], ts=T0 + timedelta(seconds=1)))
    assert portfolio.net_quantity(YES) == Decimal("10")
    assert life.state.value == "ACTIVE"


def test_invalid_order_transition() -> None:
    _, orders, _, _, _, _ = wired_shadow()
    oid = new_order_id()
    cmd = submit_buy()
    orders.create_from_command(cmd, order_id=oid)
    with pytest.raises(OrderTransitionError):
        orders.on_filled(
            OrderFilled(
                event_id=new_event_id(),
                correlation_id=cmd.correlation_id,
                causation_id=None,
                ts_event=T0,
                ts_received=T0,
                source=EventSource.SYSTEM,
                execution_id=ExecutionId("x1"),
                order_id=oid,
                instrument_id=YES,
                side=OrderSide.BUY,
                fill_quantity=Decimal("1"),
                fill_price=Decimal("0.5"),
                fee_amount=Decimal("0"),
                fee_currency="USD",
                cumulative_filled=Decimal("1"),
                remaining_quantity=Decimal("9"),
            )
        )


def test_duplicate_execution_ignored() -> None:
    disp, orders, ledger, portfolio, _, oms = wired_shadow()
    oms.on_book_updated(make_book(YES))
    oid = new_order_id()
    oms.submit(submit_buy(qty="5"), order_id=oid)
    fill = ledger.all_fills()[0]
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
    assert len(ledger.all_fills()) == 1
    assert portfolio.net_quantity(YES) == Decimal("5")
