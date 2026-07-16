"""Phase 2 market-data contracts (store v2, ingest, features)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from tyrex_pm.core.ids import TokenId


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal


class BookSource:
    """Book update origin tag."""

    WEBSOCKET = "websocket"
    REST_BOOTSTRAP = "rest_bootstrap"
    REST_RECOVERY = "rest_recovery"
    REST_POLL = "rest_poll"
    FIXTURE = "fixture"


class SourceQuality:
    """Quality tier for a book source."""

    WS_PRIMARY = "ws_primary"
    REST_BOOTSTRAP = "rest_bootstrap"
    REST_RECOVERY = "rest_recovery"
    REST_POLL = "rest_poll"


@dataclass(frozen=True)
class MarketStateSnapshot:
    """Immutable captured book state with v2 metadata."""

    token_id: TokenId
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    best_bid: Decimal | None
    best_ask: Decimal | None
    best_bid_size: Decimal | None
    best_ask_size: Decimal | None
    spread: Decimal | None
    mid: Decimal | None
    received_ts: datetime
    exchange_ts: datetime | None
    book_age_ms: int
    source: str
    source_quality: str
    sequence: int | None
    book_hash: str | None
    snapshot_id: str
    reconnect_gap: bool


@dataclass(frozen=True)
class PairMarketSnapshot:
    """Both legs captured at one instant."""

    pair_id: str
    yes: MarketStateSnapshot
    no: MarketStateSnapshot
    pair_snapshot_id: str
    captured_at: datetime


@dataclass(frozen=True)
class RawMarketEvent:
    raw_event_id: str
    channel: str
    payload: dict[str, Any]
    received_ts: datetime
    received_monotonic_ns: int
    connection_id: str
    local_event_counter: int


@dataclass(frozen=True)
class BasicFeatureSnapshot:
    snapshot_id: str
    pair_snapshot_id: str | None
    source: str
    book_age_ms: int
    spread: Decimal | None
    mid: Decimal | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    best_bid_size: Decimal | None
    best_ask_size: Decimal | None
    depth_at_size: Decimal | None
    effective_bid_at_size: Decimal | None
    effective_ask_at_size: Decimal | None
    sweep_vwap_buy: Decimal | None
    sweep_vwap_sell: Decimal | None
    quality_verdict: str | None
    missing_fields: tuple[str, ...] = field(default_factory=tuple)
