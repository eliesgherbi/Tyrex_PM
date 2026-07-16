"""Authoritative order lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum

from tyrex_pm.core.commands import SubmitOrderCommand
from tyrex_pm.core.execution_events import (
    OrderAccepted,
    OrderCanceled,
    OrderCancelPending,
    OrderFilled,
    OrderPartiallyFilled,
    OrderRejected,
    OrderSubmitted,
)
from tyrex_pm.core.ids import (
    ClientOrderId,
    CorrelationId,
    InstrumentId,
    MarketId,
    OrderId,
    StrategyId,
)
from tyrex_pm.core.intents import IntentId, OrderSide
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.planning.plan import PlanId


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


_TERMINAL = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}
)

_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset({OrderStatus.SUBMITTED, OrderStatus.REJECTED}),
    OrderStatus.SUBMITTED: frozenset(
        {OrderStatus.ACCEPTED, OrderStatus.REJECTED, OrderStatus.CANCEL_PENDING}
    ),
    OrderStatus.ACCEPTED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELED,
        }
    ),
    OrderStatus.CANCEL_PENDING: frozenset(
        {OrderStatus.CANCELED, OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


class OrderTransitionError(RuntimeError):
    pass


@dataclass
class OrderRecord:
    order_id: OrderId
    client_order_id: ClientOrderId
    plan_id: PlanId
    intent_id: IntentId
    strategy_id: StrategyId
    instrument_id: InstrumentId
    market_id: MarketId
    side: OrderSide
    quantity: Decimal
    limit_price: Decimal
    status: OrderStatus
    filled_quantity: Decimal
    remaining_quantity: Decimal
    average_fill_price: Decimal | None
    venue_order_id: str | None
    created_at: datetime
    updated_at: datetime
    correlation_id: CorrelationId
    seen_execution_ids: set[str]

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id.value,
            "client_order_id": self.client_order_id.value,
            "plan_id": self.plan_id.value,
            "intent_id": self.intent_id.value,
            "strategy_id": self.strategy_id.value,
            "instrument_id": self.instrument_id.value,
            "market_id": self.market_id.value,
            "side": self.side.value,
            "quantity": str(self.quantity),
            "limit_price": str(self.limit_price),
            "status": self.status.value,
            "filled_quantity": str(self.filled_quantity),
            "remaining_quantity": str(self.remaining_quantity),
            "average_fill_price": None
            if self.average_fill_price is None
            else str(self.average_fill_price),
            "venue_order_id": self.venue_order_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "correlation_id": self.correlation_id.value,
            "seen_execution_ids": sorted(self.seen_execution_ids),
        }


class OrderStore:
    """Owns per-order status and derived fill totals."""

    PRIORITY = 100

    def __init__(self) -> None:
        self._orders: dict[str, OrderRecord] = {}

    def attach(self, dispatcher: EventDispatcher) -> None:
        dispatcher.subscribe(OrderSubmitted, self.on_submitted, priority=self.PRIORITY)
        dispatcher.subscribe(OrderAccepted, self.on_accepted, priority=self.PRIORITY)
        dispatcher.subscribe(OrderRejected, self.on_rejected, priority=self.PRIORITY)
        dispatcher.subscribe(OrderCancelPending, self.on_cancel_pending, priority=self.PRIORITY)
        dispatcher.subscribe(OrderCanceled, self.on_canceled, priority=self.PRIORITY)
        dispatcher.subscribe(OrderPartiallyFilled, self.on_partial, priority=self.PRIORITY)
        dispatcher.subscribe(OrderFilled, self.on_filled, priority=self.PRIORITY)

    def get(self, order_id: OrderId) -> OrderRecord | None:
        return self._orders.get(order_id.value)

    def working_orders(self) -> list[OrderRecord]:
        return [
            o
            for o in self._orders.values()
            if o.status
            in {
                OrderStatus.SUBMITTED,
                OrderStatus.ACCEPTED,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.CANCEL_PENDING,
            }
        ]

    def has_pending_entry(self) -> bool:
        return any(
            o.side is OrderSide.BUY and o.status not in _TERMINAL
            for o in self._orders.values()
        )

    def create_from_command(self, command: SubmitOrderCommand, *, order_id: OrderId) -> OrderRecord:
        if order_id.value in self._orders:
            raise OrderTransitionError(f"duplicate order_id {order_id.value}")
        rec = OrderRecord(
            order_id=order_id,
            client_order_id=command.client_order_id,
            plan_id=command.plan_id,
            intent_id=command.intent_id,
            strategy_id=command.strategy_id,
            instrument_id=command.instrument_id,
            market_id=command.market_id,
            side=command.side,
            quantity=command.quantity,
            limit_price=command.limit_price,
            status=OrderStatus.CREATED,
            filled_quantity=Decimal("0"),
            remaining_quantity=command.quantity,
            average_fill_price=None,
            venue_order_id=None,
            created_at=command.created_at,
            updated_at=command.created_at,
            correlation_id=command.correlation_id,
            seen_execution_ids=set(),
        )
        self._orders[order_id.value] = rec
        return rec

    def _transition(self, order_id: OrderId, new_status: OrderStatus, *, when: datetime) -> OrderRecord:
        rec = self._orders.get(order_id.value)
        if rec is None:
            raise OrderTransitionError(f"unknown order {order_id.value}")
        allowed = _TRANSITIONS.get(rec.status, frozenset())
        if new_status not in allowed and new_status != rec.status:
            raise OrderTransitionError(
                f"invalid transition {rec.status.value} → {new_status.value} for {order_id.value}"
            )
        rec.status = new_status
        rec.updated_at = when
        return rec

    def on_submitted(self, event: OrderSubmitted) -> None:
        self._transition(event.order_id, OrderStatus.SUBMITTED, when=event.ts_event)

    def on_accepted(self, event: OrderAccepted) -> None:
        rec = self._transition(event.order_id, OrderStatus.ACCEPTED, when=event.ts_event)
        rec.venue_order_id = event.venue_order_id

    def on_rejected(self, event: OrderRejected) -> None:
        self._transition(event.order_id, OrderStatus.REJECTED, when=event.ts_event)

    def on_cancel_pending(self, event: OrderCancelPending) -> None:
        self._transition(event.order_id, OrderStatus.CANCEL_PENDING, when=event.ts_event)

    def on_canceled(self, event: OrderCanceled) -> None:
        self._transition(event.order_id, OrderStatus.CANCELED, when=event.ts_event)

    def on_partial(self, event: OrderPartiallyFilled) -> None:
        self._apply_fill(event, terminal=False)

    def on_filled(self, event: OrderFilled) -> None:
        self._apply_fill(event, terminal=True)

    def _apply_fill(self, event: OrderPartiallyFilled | OrderFilled, *, terminal: bool) -> None:
        rec = self._orders.get(event.order_id.value)
        if rec is None:
            raise OrderTransitionError(f"unknown order {event.order_id.value}")
        if event.execution_id.value in rec.seen_execution_ids:
            return  # idempotent
        if event.cumulative_filled > rec.quantity:
            raise OrderTransitionError("fill exceeds order quantity")
        if event.cumulative_filled < rec.filled_quantity:
            raise OrderTransitionError("filled quantity cannot decrease")
        # Average price update
        prev_notional = (rec.average_fill_price or Decimal("0")) * rec.filled_quantity
        new_notional = prev_notional + event.fill_quantity * event.fill_price
        new_filled = event.cumulative_filled
        rec.filled_quantity = new_filled
        rec.remaining_quantity = event.remaining_quantity
        rec.average_fill_price = None if new_filled == 0 else new_notional / new_filled
        rec.seen_execution_ids.add(event.execution_id.value)
        target = OrderStatus.FILLED if terminal else OrderStatus.PARTIALLY_FILLED
        self._transition(event.order_id, target, when=event.ts_event)

    def snapshot(self) -> list[dict]:
        return [o.to_dict() for o in self._orders.values()]

    def restore(self, rows: list[dict]) -> None:
        self._orders.clear()
        for row in rows:
            oid = OrderId(row["order_id"])
            self._orders[oid.value] = OrderRecord(
                order_id=oid,
                client_order_id=ClientOrderId(row["client_order_id"]),
                plan_id=PlanId(row["plan_id"]),
                intent_id=IntentId(row["intent_id"]),
                strategy_id=StrategyId(row["strategy_id"]),
                instrument_id=InstrumentId(row["instrument_id"]),
                market_id=MarketId(row["market_id"]),
                side=OrderSide(row["side"]),
                quantity=Decimal(row["quantity"]),
                limit_price=Decimal(row["limit_price"]),
                status=OrderStatus(row["status"]),
                filled_quantity=Decimal(row["filled_quantity"]),
                remaining_quantity=Decimal(row["remaining_quantity"]),
                average_fill_price=None
                if row["average_fill_price"] is None
                else Decimal(row["average_fill_price"]),
                venue_order_id=row.get("venue_order_id"),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
                correlation_id=CorrelationId(row.get("correlation_id") or "restored"),
                seen_execution_ids=set(row.get("seen_execution_ids") or []),
            )
