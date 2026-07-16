"""Immutable fill ledger — answers which fills happened."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tyrex_pm.core.execution_events import (
    ExecutionId,
    OrderFilled,
    OrderPartiallyFilled,
)
from tyrex_pm.core.ids import InstrumentId, OrderId
from tyrex_pm.core.intents import OrderSide
from tyrex_pm.engine.dispatcher import EventDispatcher


@dataclass(frozen=True, kw_only=True)
class FillRecord:
    execution_id: ExecutionId
    order_id: OrderId
    instrument_id: InstrumentId
    side: OrderSide
    quantity: Decimal
    price: Decimal
    fee_amount: Decimal
    fee_currency: str
    ts_event: datetime

    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id.value,
            "order_id": self.order_id.value,
            "instrument_id": self.instrument_id.value,
            "side": self.side.value,
            "quantity": str(self.quantity),
            "price": str(self.price),
            "fee_amount": str(self.fee_amount),
            "fee_currency": self.fee_currency,
            "ts_event": self.ts_event.isoformat(),
        }


class FillLedger:
    """Owns individual fills. Duplicate execution IDs are ignored."""

    PRIORITY = 100

    def __init__(self) -> None:
        self._fills: dict[str, FillRecord] = {}
        self._order: list[str] = []

    def attach(self, dispatcher: EventDispatcher) -> None:
        dispatcher.subscribe(OrderPartiallyFilled, self.on_fill, priority=self.PRIORITY)
        dispatcher.subscribe(OrderFilled, self.on_fill, priority=self.PRIORITY)

    def has(self, execution_id: ExecutionId | str) -> bool:
        key = execution_id if isinstance(execution_id, str) else execution_id.value
        return key in self._fills

    def on_fill(self, event: OrderPartiallyFilled | OrderFilled) -> None:
        key = event.execution_id.value
        if key in self._fills:
            return
        rec = FillRecord(
            execution_id=event.execution_id,
            order_id=event.order_id,
            instrument_id=event.instrument_id,
            side=event.side,
            quantity=event.fill_quantity,
            price=event.fill_price,
            fee_amount=event.fee_amount,
            fee_currency=event.fee_currency,
            ts_event=event.ts_event,
        )
        self._fills[key] = rec
        self._order.append(key)

    def all_fills(self) -> list[FillRecord]:
        return [self._fills[k] for k in self._order]

    def snapshot(self) -> list[dict]:
        return [f.to_dict() for f in self.all_fills()]

    def restore(self, rows: list[dict]) -> None:
        self._fills.clear()
        self._order.clear()
        for row in rows:
            eid = ExecutionId(row["execution_id"])
            self._fills[eid.value] = FillRecord(
                execution_id=eid,
                order_id=OrderId(row["order_id"]),
                instrument_id=InstrumentId(row["instrument_id"]),
                side=OrderSide(row["side"]),
                quantity=Decimal(row["quantity"]),
                price=Decimal(row["price"]),
                fee_amount=Decimal(row["fee_amount"]),
                fee_currency=row["fee_currency"],
                ts_event=datetime.fromisoformat(row["ts_event"]),
            )
            self._order.append(eid.value)
