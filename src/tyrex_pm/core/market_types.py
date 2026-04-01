"""Domain types for market allowlist / resolution (milestone v1.01)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AllowlistEntry:
    """One row from config/v1_markets.yaml."""

    slug: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class ResolvedMarket:
    """Output of Nautilus PolymarketDataLoader + public CLOB book probe."""

    slug: str
    instrument_id: str
    token_id: str
    price_increment: str
    size_increment: str
    neg_risk: bool | None
    minimum_tick_size: str | None
    book_status: str
    book_detail: str
    clob_tick_size: str | None
    resolved_at_utc: str
