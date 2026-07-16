"""Polymarket binary market identity (not BTC/Z-Gap specific)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import InstrumentId, MarketId, TokenId
from tyrex_pm.core.instruments import Instrument, OutcomeSide
from tyrex_pm.core.numerics import as_decimal, require_non_negative


class MarketStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, kw_only=True)
class MarketRequest:
    """How to resolve a market for observe mode."""

    event_slug: str | None = None
    event_url: str | None = None
    condition_id: str | None = None
    fixture_path: str | None = None

    def __post_init__(self) -> None:
        if not any((self.event_slug, self.event_url, self.condition_id, self.fixture_path)):
            raise ValueError("MarketRequest needs slug, url, condition_id, or fixture_path")


@dataclass(frozen=True, kw_only=True)
class BinaryMarket:
    market_id: MarketId
    condition_id: str
    question: str
    yes: Instrument
    no: Instrument
    event_start: datetime | None = None
    event_end: datetime | None = None
    tick_size: Decimal | None = None
    min_order_size: Decimal | None = None
    status: MarketStatus = MarketStatus.UNKNOWN
    event_slug: str | None = None

    def __post_init__(self) -> None:
        if self.yes.outcome is not OutcomeSide.YES:
            raise ValueError("yes instrument must have OutcomeSide.YES")
        if self.no.outcome is not OutcomeSide.NO:
            raise ValueError("no instrument must have OutcomeSide.NO")
        if self.yes.market_id != self.market_id or self.no.market_id != self.market_id:
            raise ValueError("instrument market_id must match BinaryMarket.market_id")
        if self.event_start is not None:
            object.__setattr__(
                self, "event_start", require_utc(self.event_start, field_name="event_start")
            )
        if self.event_end is not None:
            object.__setattr__(
                self, "event_end", require_utc(self.event_end, field_name="event_end")
            )
        if self.tick_size is not None:
            object.__setattr__(
                self,
                "tick_size",
                require_non_negative(
                    as_decimal(self.tick_size, field_name="tick_size"),
                    field_name="tick_size",
                ),
            )
        if self.min_order_size is not None:
            object.__setattr__(
                self,
                "min_order_size",
                require_non_negative(
                    as_decimal(self.min_order_size, field_name="min_order_size"),
                    field_name="min_order_size",
                ),
            )


def make_binary_instruments(
    *,
    market_id: MarketId,
    yes_token: TokenId,
    no_token: TokenId,
) -> tuple[Instrument, Instrument]:
    yes = Instrument(
        instrument_id=InstrumentId(yes_token.value),
        market_id=market_id,
        token_id=yes_token,
        outcome=OutcomeSide.YES,
        symbol="YES",
    )
    no = Instrument(
        instrument_id=InstrumentId(no_token.value),
        market_id=market_id,
        token_id=no_token,
        outcome=OutcomeSide.NO,
        symbol="NO",
    )
    return yes, no
