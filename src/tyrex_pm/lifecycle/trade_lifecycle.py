"""Authoritative trade lifecycle (host-owned; strategy reacts)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from tyrex_pm.core.execution_events import (
    OrderCanceled,
    OrderFilled,
    OrderPartiallyFilled,
    OrderRejected,
)
from tyrex_pm.core.ids import InstrumentId, OrderId
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.execution.order_store import OrderStatus, OrderStore
from tyrex_pm.portfolio.portfolio import Portfolio


class LifecycleState(str, Enum):
    FLAT = "FLAT"
    ENTRY_PENDING = "ENTRY_PENDING"
    ACTIVE = "ACTIVE"
    EXIT_PENDING = "EXIT_PENDING"
    TERMINAL = "TERMINAL"


class LifecycleError(RuntimeError):
    pass


@dataclass
class LifecycleSnapshot:
    state: LifecycleState
    instrument_id: InstrumentId | None
    entry_order_id: OrderId | None
    exit_order_id: OrderId | None
    activated_at: datetime | None
    updated_at: datetime | None


class TradeLifecycle:
    """Owns trade lifecycle; position truth comes from fills/portfolio."""

    PRIORITY = 80

    def __init__(self, *, order_store: OrderStore, portfolio: Portfolio) -> None:
        self._orders = order_store
        self._portfolio = portfolio
        self._state = LifecycleState.FLAT
        self._instrument_id: InstrumentId | None = None
        self._entry_order_id: OrderId | None = None
        self._exit_order_id: OrderId | None = None
        self._activated_at: datetime | None = None
        self._updated_at: datetime | None = None
        self._listeners: list = []

    @property
    def state(self) -> LifecycleState:
        return self._state

    def view(self) -> LifecycleSnapshot:
        return LifecycleSnapshot(
            state=self._state,
            instrument_id=self._instrument_id,
            entry_order_id=self._entry_order_id,
            exit_order_id=self._exit_order_id,
            activated_at=self._activated_at,
            updated_at=self._updated_at,
        )

    def on_transition(self, callback) -> None:
        self._listeners.append(callback)

    def attach(self, dispatcher: EventDispatcher) -> None:
        dispatcher.subscribe(OrderPartiallyFilled, self.on_fill, priority=self.PRIORITY)
        dispatcher.subscribe(OrderFilled, self.on_fill, priority=self.PRIORITY)
        dispatcher.subscribe(OrderRejected, self.on_rejected, priority=self.PRIORITY)
        dispatcher.subscribe(OrderCanceled, self.on_canceled, priority=self.PRIORITY)

    def note_entry_submitted(self, order_id: OrderId, instrument_id: InstrumentId, *, when: datetime) -> None:
        if self._state not in {LifecycleState.FLAT}:
            raise LifecycleError(f"cannot enter from {self._state.value}")
        self._set(LifecycleState.ENTRY_PENDING, when=when)
        self._entry_order_id = order_id
        self._instrument_id = instrument_id

    def note_exit_submitted(self, order_id: OrderId, *, when: datetime) -> None:
        if self._state not in {LifecycleState.ACTIVE}:
            raise LifecycleError(f"cannot exit from {self._state.value}")
        self._set(LifecycleState.EXIT_PENDING, when=when)
        self._exit_order_id = order_id

    def mark_terminal(self, *, when: datetime) -> None:
        self._set(LifecycleState.TERMINAL, when=when)

    def on_fill(self, event: OrderPartiallyFilled | OrderFilled) -> None:
        if self._instrument_id is None:
            self._instrument_id = event.instrument_id
        qty = self._portfolio.net_quantity(event.instrument_id)
        when = event.ts_event
        if self._state is LifecycleState.ENTRY_PENDING and qty > 0:
            self._activated_at = when
            self._set(LifecycleState.ACTIVE, when=when)
        if self._state is LifecycleState.EXIT_PENDING:
            if qty == 0:
                self._clear_trade(when=when)
            elif self._exit_order_terminal():
                # Partial exit filled/canceled with residual position.
                self._exit_order_id = None
                self._set(LifecycleState.ACTIVE, when=when)
        if self._state is LifecycleState.ACTIVE and qty == 0:
            self._clear_trade(when=when)

    def _exit_order_terminal(self) -> bool:
        if self._exit_order_id is None:
            return False
        rec = self._orders.get(self._exit_order_id)
        if rec is None:
            return False
        return rec.status in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
        }

    def on_rejected(self, event: OrderRejected) -> None:
        when = event.ts_event
        if self._entry_order_id and event.order_id.value == self._entry_order_id.value:
            if self._state is LifecycleState.ENTRY_PENDING:
                qty = (
                    self._portfolio.net_quantity(self._instrument_id)
                    if self._instrument_id
                    else 0
                )
                if qty > 0:
                    self._set(LifecycleState.ACTIVE, when=when)
                else:
                    self._clear_trade(when=when)
        if self._exit_order_id and event.order_id.value == self._exit_order_id.value:
            if self._state is LifecycleState.EXIT_PENDING:
                self._set(LifecycleState.ACTIVE, when=when)

    def on_canceled(self, event: OrderCanceled) -> None:
        when = event.ts_event
        rec = self._orders.get(event.order_id)
        filled = Decimal("0") if rec is None else rec.filled_quantity
        if self._entry_order_id and event.order_id.value == self._entry_order_id.value:
            if self._state is LifecycleState.ENTRY_PENDING:
                if filled > 0 or (
                    self._instrument_id
                    and self._portfolio.net_quantity(self._instrument_id) > 0
                ):
                    self._activated_at = when
                    self._set(LifecycleState.ACTIVE, when=when)
                else:
                    self._clear_trade(when=when)
        if self._exit_order_id and event.order_id.value == self._exit_order_id.value:
            if self._state is LifecycleState.EXIT_PENDING:
                qty = (
                    self._portfolio.net_quantity(self._instrument_id)
                    if self._instrument_id
                    else filled
                )
                if qty == 0:
                    self._clear_trade(when=when)
                else:
                    self._set(LifecycleState.ACTIVE, when=when)

    def _clear_trade(self, *, when: datetime) -> None:
        self._entry_order_id = None
        self._exit_order_id = None
        self._instrument_id = None
        self._activated_at = None
        self._set(LifecycleState.FLAT, when=when)

    def _set(self, state: LifecycleState, *, when: datetime) -> None:
        prev = self._state
        self._state = state
        self._updated_at = when
        for cb in self._listeners:
            cb(prev, state, when)

    def snapshot(self) -> dict:
        return {
            "state": self._state.value,
            "instrument_id": None if self._instrument_id is None else self._instrument_id.value,
            "entry_order_id": None if self._entry_order_id is None else self._entry_order_id.value,
            "exit_order_id": None if self._exit_order_id is None else self._exit_order_id.value,
            "activated_at": None if self._activated_at is None else self._activated_at.isoformat(),
            "updated_at": None if self._updated_at is None else self._updated_at.isoformat(),
        }

    def restore(self, data: dict) -> None:
        self._state = LifecycleState(data["state"])
        self._instrument_id = (
            None if data.get("instrument_id") is None else InstrumentId(data["instrument_id"])
        )
        self._entry_order_id = (
            None if data.get("entry_order_id") is None else OrderId(data["entry_order_id"])
        )
        self._exit_order_id = (
            None if data.get("exit_order_id") is None else OrderId(data["exit_order_id"])
        )
        self._activated_at = (
            None
            if data.get("activated_at") is None
            else datetime.fromisoformat(data["activated_at"])
        )
        self._updated_at = (
            None
            if data.get("updated_at") is None
            else datetime.fromisoformat(data["updated_at"])
        )
