"""best_bid_ask table rows (M2B.3-A)."""

from __future__ import annotations

from typing import Any

from tyrex_pm.core.events import EventType, MarketEvent

from research.normalize.io import iso_ts, payload_raw_json


def rows_from_event(date: str, market_id: str, event: MarketEvent) -> list[dict[str, Any]]:
    if event.event_type != EventType.BEST_BID_ASK:
        return []
    raw = event.payload.get("raw") or event.payload
    if not isinstance(raw, dict):
        raw = {}
    return [
        {
            "date": date,
            "market_id": market_id,
            "token_id": str(event.token_id) if event.token_id is not None else raw.get("asset_id"),
            "event_id": event.event_id,
            "recv_ts": iso_ts(event.recv_ts),
            "source_ts": iso_ts(event.source_ts),
            "best_bid": str(raw.get("best_bid")) if raw.get("best_bid") is not None else None,
            "best_ask": str(raw.get("best_ask")) if raw.get("best_ask") is not None else None,
            "spread": str(raw.get("spread")) if raw.get("spread") is not None else None,
            "raw_json": payload_raw_json(event.payload),
        }
    ]
