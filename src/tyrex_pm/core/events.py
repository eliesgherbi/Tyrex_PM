"""Phase 2B canonical market event contract (M2B.0-A).

Frozen :class:`MarketEvent` records are the single interchange format for
ingestion, projection, recording, and replay. See ``Docs/EVENT_MODEL.md``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from tyrex_pm.core.ids import TokenId

MARKET_EVENT_SCHEMA_VERSION = 1

__all__ = [
    "MARKET_EVENT_SCHEMA_VERSION",
    "EventType",
    "MarketEvent",
    "canonical_payload_json",
    "compute_event_id",
    "compute_payload_digest",
    "event_from_dict",
    "event_to_dict",
]


class EventType(str, Enum):
    BOOK_SNAPSHOT = "book_snapshot"
    BOOK_DELTA = "book_delta"
    TRADE_PRINT = "trade_print"
    BOOK_QUALITY_CHANGED = "book_quality_changed"
    WS_SEQ_GAP = "ws_seq_gap"
    WS_RECONNECT = "ws_reconnect"
    REST_RECOVERY_USED = "rest_recovery_used"
    OUT_OF_ORDER_EVENT = "out_of_order_event"
    MARKET_DISCOVERED = "market_discovered"
    MARKET_METADATA_UPDATED = "market_metadata_updated"
    MARKET_PHASE_CHANGED = "market_phase_changed"
    SESSION_TIMER_TICK = "session_timer_tick"
    MARKET_CLOSE_APPROACHING = "market_close_approaching"
    MARKET_CLOSED = "market_closed"
    RESOLUTION_OBSERVED = "resolution_observed"
    EXTERNAL_BTC_TICK = "external_btc_tick"
    CLOCK_SYNC = "clock_sync"
    LAST_TRADE_PRICE = "last_trade_price"
    TICK_SIZE_CHANGE = "tick_size_change"
    BEST_BID_ASK = "best_bid_ask"
    NEW_MARKET = "new_market"
    MARKET_RESOLVED = "market_resolved"
    REFERENCE_PRICE_TICK = "reference_price_tick"
    PRICE_TO_BEAT_OBSERVED = "price_to_beat_observed"


_BOOK_EVENT_TYPES = frozenset({EventType.BOOK_SNAPSHOT, EventType.BOOK_DELTA})


@dataclass(frozen=True)
class MarketEvent:
    event_id: str
    event_type: EventType
    market_id: str | None
    token_id: TokenId | None
    venue_cursor: str | None
    source_ts: datetime | None
    recv_ts: datetime
    payload: Mapping[str, Any]
    schema_version: int = MARKET_EVENT_SCHEMA_VERSION
    connection_id: str | None = None
    local_counter: int | None = None

    def is_book_event(self) -> bool:
        return self.event_type in _BOOK_EVENT_TYPES


def _stable_json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=timezone.utc)
        return obj.astimezone(timezone.utc).isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, TokenId):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def canonical_payload_json(payload: Mapping[str, Any]) -> str:
    """Deterministic JSON for digesting — sort_keys, compact separators."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_stable_json_default)


def compute_payload_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_payload_json(payload).encode("utf-8")).hexdigest()


def compute_event_id(
    *,
    event_type: str,
    token_id: str | None,
    venue_cursor: str | None,
    source_ts: datetime | None,
    payload_digest: str,
) -> str:
    source_ms = int(source_ts.timestamp() * 1000) if source_ts is not None else 0
    key = f"{event_type}|{token_id or ''}|{venue_cursor or ''}|{source_ms}|{payload_digest}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _format_dt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(raw: object) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw.astimezone(timezone.utc)
    text = str(raw).strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def event_to_dict(event: MarketEvent) -> dict[str, Any]:
    return {
        "schema_version": event.schema_version,
        "event_id": event.event_id,
        "event_type": event.event_type.value,
        "market_id": event.market_id,
        "token_id": str(event.token_id) if event.token_id is not None else None,
        "venue_cursor": event.venue_cursor,
        "source_ts": _format_dt(event.source_ts),
        "recv_ts": _format_dt(event.recv_ts),
        "payload": dict(event.payload),
        "connection_id": event.connection_id,
        "local_counter": event.local_counter,
    }


def event_from_dict(data: Mapping[str, Any]) -> MarketEvent:
    token_raw = data.get("token_id")
    token_id = TokenId(str(token_raw)) if token_raw not in (None, "") else None
    recv_ts = _parse_dt(data.get("recv_ts"))
    if recv_ts is None:
        raise ValueError("recv_ts is required")
    return MarketEvent(
        event_id=str(data["event_id"]),
        event_type=EventType(str(data["event_type"])),
        market_id=data.get("market_id"),
        token_id=token_id,
        venue_cursor=data.get("venue_cursor"),
        source_ts=_parse_dt(data.get("source_ts")),
        recv_ts=recv_ts,
        payload=dict(data.get("payload") or {}),
        schema_version=int(data.get("schema_version", MARKET_EVENT_SCHEMA_VERSION)),
        connection_id=data.get("connection_id"),
        local_counter=data.get("local_counter"),
    )
