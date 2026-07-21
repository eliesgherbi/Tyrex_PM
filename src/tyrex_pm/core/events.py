"""Typed market/time events for the R2/R3 read-only path.

Execution events (orders/fills) are deferred to R5.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from decimal import Decimal

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import CorrelationId, EventId
from tyrex_pm.core.ingress import IngressMeta
from tyrex_pm.core.snapshots import BookSnapshot, ReferencePriceSnapshot, SettlementReferenceSnapshot


class EventSource(str, Enum):
    POLYMARKET_CLOB = "polymarket_clob"
    POLYMARKET_RTDS_CHAINLINK = "polymarket_rtds_chainlink"
    POLYMARKET_RTDS_BINANCE = "polymarket_rtds_binance"
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
    * ``ts_event`` — provider/source occurrence time (UTC).
    * ``ts_received`` — **raw** local host wall UTC at ingress (uncorrected).
      Corrected receive time lives on ``IngressMeta.receive_wall_corrected_utc``;
      never write corrected time into ``ts_received``.
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
    """External trading/comparison reference price update (never settlement truth)."""

    reference: ReferencePriceSnapshot
    ingress: IngressMeta | None = None

    def __post_init__(self) -> None:
        _validate_event_times(self)


@dataclass(frozen=True, kw_only=True)
class SettlementReferenceUpdated(Event):
    """Settlement-associated reference tick (e.g. RTDS Chainlink).

    Distinct from Binance ``ReferencePriceUpdated``. Does not select or lock PTB.
    """

    settlement: SettlementReferenceSnapshot
    ingress: IngressMeta | None = None

    def __post_init__(self) -> None:
        _validate_event_times(self)
        if self.source not in (
            EventSource.POLYMARKET_RTDS_CHAINLINK,
            EventSource.TEST,
            EventSource.SYSTEM,
        ):
            raise ValueError(
                "SettlementReferenceUpdated.source must be POLYMARKET_RTDS_CHAINLINK "
                "(or TEST/SYSTEM in tests)"
            )
        if self.settlement.price <= Decimal("0"):
            raise ValueError("settlement price must be positive")


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
