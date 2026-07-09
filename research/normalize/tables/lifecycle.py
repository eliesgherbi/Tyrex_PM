"""lifecycle_events table rows (M2B.3)."""

from __future__ import annotations

from typing import Any

from tyrex_pm.core.events import EventType, MarketEvent

from research.normalize.io import iso_ts, payload_raw_json

_LIFECYCLE_TYPES = frozenset(
    {
        EventType.MARKET_DISCOVERED,
        EventType.WS_SEQ_GAP,
        EventType.WS_RECONNECT,
        EventType.MARKET_METADATA_UPDATED,
        EventType.MARKET_PHASE_CHANGED,
        EventType.MARKET_CLOSED,
        EventType.RESOLUTION_OBSERVED,
        EventType.REST_RECOVERY_USED,
        EventType.OUT_OF_ORDER_EVENT,
        EventType.NEW_MARKET,
    }
)

_EXCLUDED_FROM_LIFECYCLE = frozenset(
    {
        EventType.BOOK_SNAPSHOT,
        EventType.BOOK_DELTA,
        EventType.EXTERNAL_BTC_TICK,
        EventType.CLOCK_SYNC,
        EventType.LAST_TRADE_PRICE,
        EventType.TICK_SIZE_CHANGE,
        EventType.BEST_BID_ASK,
        EventType.MARKET_RESOLVED,
        EventType.REFERENCE_PRICE_TICK,
        EventType.PRICE_TO_BEAT_OBSERVED,
    }
)


def rows_from_event(date: str, market_id: str | None, event: MarketEvent, *, source: str = "polymarket") -> list[dict[str, Any]]:
    if event.event_type in _EXCLUDED_FROM_LIFECYCLE:
        return []
    if event.event_type not in _LIFECYCLE_TYPES and event.event_type != EventType.TRADE_PRINT:
        return []
    return [
        {
            "date": date,
            "market_id": market_id or event.market_id,
            "source": source,
            "event_type": event.event_type.value,
            "event_id": event.event_id,
            "recv_ts": iso_ts(event.recv_ts),
            "source_ts": iso_ts(event.source_ts),
            "payload_json": payload_raw_json(event.payload),
        }
    ]
