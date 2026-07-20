"""Authoritative trade lifecycle (host-owned; strategy reacts).

R5.1 states:
  FLAT → ENTRY_PENDING → ACTIVE → EXIT_REQUESTED → EXIT_PENDING → FLAT
                              ↘ EXIT_RETRY_WAIT ↗
                              → MANUAL_INTERVENTION (residual, unrecoverable)
  FLAT → TERMINAL (window end, flat only)
"""

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
    EXIT_REQUESTED = "EXIT_REQUESTED"
    EXIT_PENDING = "EXIT_PENDING"
    EXIT_RETRY_WAIT = "EXIT_RETRY_WAIT"
    # Resolution-aware SHADOW (F5): committed hold through binary settlement
    RESOLUTION_PENDING = "RESOLUTION_PENDING"
    RESOLUTION_CONFIRMED = "RESOLUTION_CONFIRMED"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"
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
    resolution_committed: bool = False
    resolution_window_id: str | None = None
    resolution_evidence_id: str | None = None
    settlement_applied_id: str | None = None


_EXIT_BUSY = frozenset(
    {
        LifecycleState.EXIT_REQUESTED,
        LifecycleState.EXIT_PENDING,
        LifecycleState.EXIT_RETRY_WAIT,
        LifecycleState.MANUAL_INTERVENTION,
        LifecycleState.RESOLUTION_PENDING,
    }
)

_RESOLUTION_PENDING_LIKE = frozenset(
    {
        LifecycleState.RESOLUTION_PENDING,
        LifecycleState.RESOLUTION_CONFIRMED,
    }
)


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
        self._resolution_committed: bool = False
        self._resolution_window_id: str | None = None
        self._resolution_evidence_id: str | None = None
        self._settlement_applied_id: str | None = None
        self._listeners: list = []

    @property
    def state(self) -> LifecycleState:
        return self._state

    def exit_busy(self) -> bool:
        return self._state in _EXIT_BUSY

    def resolution_committed(self) -> bool:
        return self._resolution_committed

    def view(self) -> LifecycleSnapshot:
        return LifecycleSnapshot(
            state=self._state,
            instrument_id=self._instrument_id,
            entry_order_id=self._entry_order_id,
            exit_order_id=self._exit_order_id,
            activated_at=self._activated_at,
            updated_at=self._updated_at,
            resolution_committed=self._resolution_committed,
            resolution_window_id=self._resolution_window_id,
            resolution_evidence_id=self._resolution_evidence_id,
            settlement_applied_id=self._settlement_applied_id,
        )

    def on_transition(self, callback) -> None:
        self._listeners.append(callback)

    def attach(self, dispatcher: EventDispatcher) -> None:
        dispatcher.subscribe(OrderPartiallyFilled, self.on_fill, priority=self.PRIORITY)
        dispatcher.subscribe(OrderFilled, self.on_fill, priority=self.PRIORITY)
        dispatcher.subscribe(OrderRejected, self.on_rejected, priority=self.PRIORITY)
        dispatcher.subscribe(OrderCanceled, self.on_canceled, priority=self.PRIORITY)

    def note_entry_submitted(
        self, order_id: OrderId, instrument_id: InstrumentId, *, when: datetime
    ) -> None:
        if self._state not in {LifecycleState.FLAT}:
            raise LifecycleError(f"cannot enter from {self._state.value}")
        self._set(LifecycleState.ENTRY_PENDING, when=when)
        self._entry_order_id = order_id
        self._instrument_id = instrument_id

    def note_exit_requested(self, *, when: datetime) -> None:
        if self._state not in {
            LifecycleState.ACTIVE,
            LifecycleState.EXIT_RETRY_WAIT,
            LifecycleState.EXIT_REQUESTED,
            # Pre-PONR: may still sell out of resolution-pending
            LifecycleState.RESOLUTION_PENDING,
        }:
            raise LifecycleError(f"cannot request exit from {self._state.value}")
        if self._state is LifecycleState.RESOLUTION_PENDING:
            self._resolution_committed = False
            self._resolution_window_id = None
        self._set(LifecycleState.EXIT_REQUESTED, when=when)

    def note_exit_submitted(self, order_id: OrderId, *, when: datetime) -> None:
        if self._state not in {
            LifecycleState.ACTIVE,
            LifecycleState.EXIT_REQUESTED,
            LifecycleState.EXIT_RETRY_WAIT,
            LifecycleState.RESOLUTION_PENDING,
        }:
            raise LifecycleError(f"cannot exit from {self._state.value}")
        if self._state is LifecycleState.RESOLUTION_PENDING:
            self._resolution_committed = False
            self._resolution_window_id = None
        self._set(LifecycleState.EXIT_PENDING, when=when)
        self._exit_order_id = order_id

    def note_exit_retry_wait(self, *, when: datetime) -> None:
        if self._state not in {
            LifecycleState.EXIT_REQUESTED,
            LifecycleState.EXIT_PENDING,
            LifecycleState.ACTIVE,
        }:
            raise LifecycleError(f"cannot exit-retry from {self._state.value}")
        self._exit_order_id = None
        self._set(LifecycleState.EXIT_RETRY_WAIT, when=when)

    def note_manual_intervention(self, *, when: datetime) -> None:
        self._set(LifecycleState.MANUAL_INTERVENTION, when=when)

    def note_resolution_committed(self, *, window_id: str, when: datetime) -> None:
        """Accept HoldToResolutionIntent → RESOLUTION_PENDING (framework-owned)."""
        if self._state not in {LifecycleState.ACTIVE, LifecycleState.RESOLUTION_PENDING}:
            raise LifecycleError(
                f"cannot commit resolution from {self._state.value}"
            )
        if self._resolution_committed and self._state is LifecycleState.RESOLUTION_PENDING:
            # Idempotent re-accept of the same commitment
            self._updated_at = when
            return
        if not window_id.strip():
            raise LifecycleError("window_id required for resolution commitment")
        self._resolution_committed = True
        self._resolution_window_id = window_id
        self._set(LifecycleState.RESOLUTION_PENDING, when=when)

    def note_resolution_evidence_accepted(self, *, evidence_id: str, when: datetime) -> None:
        if self._resolution_evidence_id == evidence_id and self._state in {
            LifecycleState.RESOLUTION_CONFIRMED,
            LifecycleState.FLAT,
        }:
            return
        if self._state is not LifecycleState.RESOLUTION_PENDING:
            raise LifecycleError(
                f"cannot accept resolution evidence from {self._state.value}"
            )
        if self._resolution_evidence_id is not None:
            raise LifecycleError("resolution evidence already accepted")
        self._resolution_evidence_id = evidence_id
        self._set(LifecycleState.RESOLUTION_CONFIRMED, when=when)

    def note_resolution_settled(self, *, settlement_id: str, when: datetime) -> None:
        """After simulated payout applied — return to FLAT (idempotent)."""
        if self._settlement_applied_id == settlement_id:
            return
        if self._state is LifecycleState.FLAT and self._settlement_applied_id is not None:
            return
        if self._state not in _RESOLUTION_PENDING_LIKE and self._state is not LifecycleState.FLAT:
            raise LifecycleError(
                f"cannot settle resolution from {self._state.value}"
            )
        self._settlement_applied_id = settlement_id
        self._resolution_committed = False
        self._clear_trade(when=when)

    def mark_terminal(self, *, when: datetime) -> None:
        if self._state not in {LifecycleState.FLAT}:
            raise LifecycleError(
                f"cannot mark TERMINAL from {self._state.value} with possible exposure"
            )
        self._set(LifecycleState.TERMINAL, when=when)

    def on_fill(self, event: OrderPartiallyFilled | OrderFilled) -> None:
        if self._instrument_id is None:
            self._instrument_id = event.instrument_id
        qty = self._portfolio.net_quantity(event.instrument_id)
        when = event.ts_event
        if self._state is LifecycleState.ENTRY_PENDING and qty > 0:
            self._activated_at = when
            self._set(LifecycleState.ACTIVE, when=when)
        if self._state in {
            LifecycleState.EXIT_PENDING,
            LifecycleState.EXIT_REQUESTED,
            LifecycleState.EXIT_RETRY_WAIT,
        }:
            if qty == 0:
                self._clear_trade(when=when)
            elif self._exit_order_terminal():
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
                    else Decimal("0")
                )
                if qty > 0:
                    self._set(LifecycleState.ACTIVE, when=when)
                else:
                    self._clear_trade(when=when)
        if self._exit_order_id and event.order_id.value == self._exit_order_id.value:
            if self._state is LifecycleState.EXIT_PENDING:
                self._exit_order_id = None
                self._set(LifecycleState.EXIT_RETRY_WAIT, when=when)

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
                    self._exit_order_id = None
                    self._set(LifecycleState.EXIT_RETRY_WAIT, when=when)

    def _clear_trade(self, *, when: datetime) -> None:
        self._entry_order_id = None
        self._exit_order_id = None
        self._instrument_id = None
        self._activated_at = None
        self._resolution_committed = False
        self._resolution_window_id = None
        # Keep settlement_applied_id / evidence_id for idempotency across restore.
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
            "resolution_committed": self._resolution_committed,
            "resolution_window_id": self._resolution_window_id,
            "resolution_evidence_id": self._resolution_evidence_id,
            "settlement_applied_id": self._settlement_applied_id,
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
        self._resolution_committed = bool(data.get("resolution_committed", False))
        self._resolution_window_id = data.get("resolution_window_id")
        self._resolution_evidence_id = data.get("resolution_evidence_id")
        self._settlement_applied_id = data.get("settlement_applied_id")
