"""market_resolutions table rows (M2B.3-A)."""

from __future__ import annotations

from typing import Any

from tyrex_pm.core.events import EventType, MarketEvent

from research.normalize.io import iso_ts, payload_raw_json


def rows_from_event(date: str, market_id: str, event: MarketEvent) -> list[dict[str, Any]]:
    if event.event_type != EventType.MARKET_RESOLVED:
        return []
    raw = event.payload.get("raw") or event.payload
    if not isinstance(raw, dict):
        raw = {}
    return [
        {
            "date": date,
            "market_id": market_id,
            "event_id": event.event_id,
            "recv_ts": iso_ts(event.recv_ts),
            "source_ts": iso_ts(event.source_ts),
            "winning_asset_id": raw.get("winning_asset_id"),
            "winning_outcome": raw.get("winning_outcome"),
            "resolved_ts": iso_ts(event.source_ts),
            "raw_json": payload_raw_json(event.payload),
        }
    ]
