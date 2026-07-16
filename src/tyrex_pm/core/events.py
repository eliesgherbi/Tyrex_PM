"""Typed market/time events for the R2/R3 read-only path.

Execution events (orders/fills) are deferred to R5.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import CorrelationId, EventId
from tyrex_pm.core.snapshots import BookSnapshot, ReferencePriceSnapshot


class EventSource(str, Enum):
    POLYMARKET_CLOB = "polymarket_clob"
    BINANCE = "binance"
    TIMER = "timer"
    SYSTEM = "system"
    TEST = "test"


def _validate_event_times(event: Event) -> None:
    object.__setattr__(event, "ts_event", require_utc(event.ts_event, field_name="ts_event"))
    object.__setattr__(
        event, "ts_received", require_utc(event.ts_received, field_name="ts_received")
    )


@dataclass(frozen=True, kw_only=True)
class Event:
    """Immutable base event with causality metadata.

    * ``event_id`` — unique identity of this event.
    * ``correlation_id`` — broader decision/operation chain.
    * ``causation_id`` — direct cause event id; None for root external events.
    * ``ts_event`` — source occurrence time (UTC).
    * ``ts_received`` — local ingestion/creation time (UTC).
    * ``source`` — validated origin identifier.
    """

    event_id: EventId
    correlation_id: CorrelationId
    ts_event: datetime
    ts_received: datetime
    source: EventSource
    causation_id: EventId | None = None

    def __post_init__(self) -> None:
        _validate_event_times(self)


@dataclass(frozen=True, kw_only=True)
class BookUpdated(Event):
    """Complete book snapshot for one instrument (not a delta)."""

    book: BookSnapshot

    def __post_init__(self) -> None:
        _validate_event_times(self)


@dataclass(frozen=True, kw_only=True)
class ReferencePriceUpdated(Event):
    """External reference price update."""

    reference: ReferencePriceSnapshot

    def __post_init__(self) -> None:
        _validate_event_times(self)


@dataclass(frozen=True, kw_only=True)
class TimerElapsed(Event):
    """Clock/timer fire for scheduling and strategy on_timer paths."""

    timer_name: str
    fire_count: int = 1

    def __post_init__(self) -> None:
        _validate_event_times(self)
        if not self.timer_name.strip():
            raise ValueError("timer_name must be non-empty")
        if self.fire_count < 1:
            raise ValueError("fire_count must be >= 1")
        if self.source not in (EventSource.TIMER, EventSource.TEST):
            raise ValueError("TimerElapsed.source must be TIMER (or TEST in tests)")
