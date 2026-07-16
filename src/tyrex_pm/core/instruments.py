"""Minimal instrument representation for R3 market-data consumers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from tyrex_pm.core.ids import InstrumentId, MarketId, TokenId


class OutcomeSide(str, Enum):
    """Binary outcome leg relative to a Polymarket market."""

    YES = "YES"
    NO = "NO"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Instrument:
    """A tradable outcome token mapped into the framework.

    ``market_id`` identifies the shared condition/market.
    ``token_id`` identifies the CLOB token.
    ``instrument_id`` is the framework key (defaults to the token string).
    """

    instrument_id: InstrumentId
    market_id: MarketId
    token_id: TokenId
    outcome: OutcomeSide = OutcomeSide.UNKNOWN
    symbol: str | None = None
