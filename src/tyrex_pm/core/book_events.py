"""Venue book ingress events (R3 Option B: snapshot + delta).

The market-state store reconstructs authoritative books. Adapters normalize
only; they do not own reconstructed mutable books as the system of record.
``BookUpdated`` remains available as a store-emitted complete snapshot view.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from tyrex_pm.core.events import Event, _validate_event_times
from tyrex_pm.core.ids import InstrumentId, MarketId
from tyrex_pm.core.numerics import as_decimal, require_non_negative, require_polymarket_price
from tyrex_pm.core.snapshots import BookSnapshot


class BookSide(str, Enum):
    BID = "BID"
    ASK = "ASK"


@dataclass(frozen=True, kw_only=True)
class BookLevelDelta:
    instrument_id: InstrumentId
    side: BookSide
    price: Decimal
    size: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "price",
            require_polymarket_price(as_decimal(self.price, field_name="price")),
        )
        object.__setattr__(
            self,
            "size",
            require_non_negative(
                as_decimal(self.size, field_name="size"),
                field_name="size",
            ),
        )


@dataclass(frozen=True, kw_only=True)
class BookSnapshotReceived(Event):
    """Full venue book snapshot (Polymarket ``event_type=book``)."""

    book: BookSnapshot
    market_id: MarketId | None = None
    venue_hash: str | None = None
    connection_epoch: int = 0

    def __post_init__(self) -> None:
        _validate_event_times(self)


@dataclass(frozen=True, kw_only=True)
class BookDeltaReceived(Event):
    """Incremental level updates (Polymarket ``price_change``)."""

    changes: tuple[BookLevelDelta, ...]
    market_id: MarketId | None = None
    connection_epoch: int = 0

    def __post_init__(self) -> None:
        _validate_event_times(self)
        if not self.changes:
            raise ValueError("BookDeltaReceived.changes must be non-empty")


@dataclass(frozen=True, kw_only=True)
class TickSizeChanged(Event):
    """Polymarket ``tick_size_change``."""

    instrument_id: InstrumentId
    old_tick_size: Decimal
    new_tick_size: Decimal
    market_id: MarketId | None = None
    connection_epoch: int = 0

    def __post_init__(self) -> None:
        _validate_event_times(self)
        object.__setattr__(
            self,
            "old_tick_size",
            require_non_negative(
                as_decimal(self.old_tick_size, field_name="old_tick_size"),
                field_name="old_tick_size",
            ),
        )
        object.__setattr__(
            self,
            "new_tick_size",
            require_non_negative(
                as_decimal(self.new_tick_size, field_name="new_tick_size"),
                field_name="new_tick_size",
            ),
        )
        if self.new_tick_size <= 0:
            raise ValueError("new_tick_size must be > 0")
