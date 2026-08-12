"""Generic instrument-keyed L2 depth (any venue; prices are not Polymarket-bounded)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import InstrumentId
from tyrex_pm.core.numerics import as_decimal, require_non_negative


@dataclass(frozen=True, slots=True)
class DepthLevel:
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", as_decimal(self.price, field_name="price"))
        if self.price <= 0:
            raise ValueError("depth price must be > 0")
        object.__setattr__(
            self,
            "quantity",
            require_non_negative(
                as_decimal(self.quantity, field_name="quantity"),
                field_name="quantity",
            ),
        )


def _bids(levels: tuple[DepthLevel, ...]) -> tuple[DepthLevel, ...]:
    seen: set[Decimal] = set()
    ordered: list[DepthLevel] = []
    for level in sorted(levels, key=lambda item: item.price, reverse=True):
        if level.price in seen:
            continue
        seen.add(level.price)
        ordered.append(level)
    return tuple(ordered)


def _asks(levels: tuple[DepthLevel, ...]) -> tuple[DepthLevel, ...]:
    seen: set[Decimal] = set()
    ordered: list[DepthLevel] = []
    for level in sorted(levels, key=lambda item: item.price):
        if level.price in seen:
            continue
        seen.add(level.price)
        ordered.append(level)
    return tuple(ordered)


@dataclass(frozen=True, slots=True, kw_only=True)
class DepthSnapshot:
    instrument_id: InstrumentId
    ts_event: datetime
    bids: tuple[DepthLevel, ...] = ()
    asks: tuple[DepthLevel, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "ts_event", require_utc(self.ts_event, field_name="ts_event")
        )
        object.__setattr__(self, "bids", _bids(self.bids))
        object.__setattr__(self, "asks", _asks(self.asks))

    @property
    def best_bid(self) -> DepthLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> DepthLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid.price + self.best_ask.price) / Decimal("2")


@dataclass
class DepthStore:
    """Latest L2 snapshot per instrument. Indicators read from here."""

    _books: dict[str, DepthSnapshot] = field(default_factory=dict)

    def apply(self, snapshot: DepthSnapshot) -> DepthSnapshot:
        self._books[snapshot.instrument_id.value] = snapshot
        return snapshot

    def capture(self, instrument_id: InstrumentId) -> DepthSnapshot | None:
        return self._books.get(instrument_id.value)

    def clear(self) -> None:
        self._books.clear()
