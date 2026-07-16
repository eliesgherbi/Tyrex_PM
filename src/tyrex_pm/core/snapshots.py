"""Immutable market snapshots for R3.

BookUpdated carries a **complete normalized snapshot**, not a delta.
Rationale: safer for the first read-only path; avoids pretending sequence
numbers / gap recovery exist before adapters implement them. Deltas may be
added later as a separate event type if needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import InstrumentId
from tyrex_pm.core.numerics import (
    DecimalLike,
    as_decimal,
    require_non_negative,
    require_polymarket_price,
)


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "price",
            require_polymarket_price(as_decimal(self.price, field_name="price")),
        )
        object.__setattr__(
            self,
            "quantity",
            require_non_negative(
                as_decimal(self.quantity, field_name="quantity"),
                field_name="quantity",
            ),
        )


def _normalize_bids(levels: tuple[BookLevel, ...]) -> tuple[BookLevel, ...]:
    # Descending price; first occurrence wins for duplicate prices.
    seen: set[Decimal] = set()
    ordered: list[BookLevel] = []
    for level in sorted(levels, key=lambda item: item.price, reverse=True):
        if level.price in seen:
            continue
        seen.add(level.price)
        ordered.append(level)
    return tuple(ordered)


def _normalize_asks(levels: tuple[BookLevel, ...]) -> tuple[BookLevel, ...]:
    # Ascending price; first occurrence wins for duplicate prices.
    seen: set[Decimal] = set()
    ordered: list[BookLevel] = []
    for level in sorted(levels, key=lambda item: item.price):
        if level.price in seen:
            continue
        seen.add(level.price)
        ordered.append(level)
    return tuple(ordered)


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """Complete top-of-book / depth snapshot for one instrument.

    Empty sides are represented as empty tuples. Best bid/ask may be None.
    Executable VWAP is **not** included; R3 market-data derives it.
    """

    instrument_id: InstrumentId
    ts_event: datetime
    bids: tuple[BookLevel, ...] = ()
    asks: tuple[BookLevel, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts_event", require_utc(self.ts_event, field_name="ts_event"))
        object.__setattr__(self, "bids", _normalize_bids(tuple(self.bids)))
        object.__setattr__(self, "asks", _normalize_asks(tuple(self.asks)))
        if self.bids and self.asks and self.bids[0].price > self.asks[0].price:
            raise ValueError(
                f"crossed book: best bid {self.bids[0].price} > best ask {self.asks[0].price}"
            )

    @property
    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None

    @classmethod
    def from_levels(
        cls,
        *,
        instrument_id: InstrumentId,
        ts_event: datetime,
        bids: list[tuple[DecimalLike, DecimalLike]] | None = None,
        asks: list[tuple[DecimalLike, DecimalLike]] | None = None,
    ) -> BookSnapshot:
        bid_levels = tuple(
            BookLevel(price=p, quantity=q) for p, q in (bids or [])
        )
        ask_levels = tuple(
            BookLevel(price=p, quantity=q) for p, q in (asks or [])
        )
        return cls(
            instrument_id=instrument_id,
            ts_event=ts_event,
            bids=bid_levels,
            asks=ask_levels,
        )


@dataclass(frozen=True, slots=True)
class ReferencePriceSnapshot:
    """External reference price (e.g. Binance BTC) for one symbol."""

    symbol: str
    price: Decimal
    ts_event: datetime
    venue: str = "binance"

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol must be non-empty")
        object.__setattr__(
            self,
            "price",
            require_non_negative(
                as_decimal(self.price, field_name="price"),
                field_name="price",
            ),
        )
        object.__setattr__(self, "ts_event", require_utc(self.ts_event, field_name="ts_event"))
