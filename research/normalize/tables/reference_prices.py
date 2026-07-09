"""reference_prices and price_to_beat table rows (M2B.3-A)."""

from __future__ import annotations

from typing import Any

from tyrex_pm.core.events import EventType, MarketEvent

from research.normalize.io import iso_ts, payload_raw_json


def reference_price_rows_from_event(date: str, event: MarketEvent) -> list[dict[str, Any]]:
    if event.event_type != EventType.REFERENCE_PRICE_TICK:
        return []
    p = event.payload
    return [
        {
            "date": date,
            "source": p.get("source"),
            "feed": p.get("feed"),
            "symbol": p.get("symbol"),
            "event_id": event.event_id,
            "recv_ts": iso_ts(event.recv_ts),
            "source_ts": iso_ts(event.source_ts) or p.get("source_event_ts"),
            "value": p.get("value"),
            "latency_ms": p.get("latency_ms"),
            "raw_json": payload_raw_json(p),
        }
    ]


def price_to_beat_rows_from_event(date: str, event: MarketEvent) -> list[dict[str, Any]]:
    if event.event_type != EventType.PRICE_TO_BEAT_OBSERVED:
        return []
    p = event.payload
    return [
        {
            "date": date,
            "market_id": p.get("market_id") or event.market_id,
            "event_start_ts": p.get("event_start_ts"),
            "event_end_ts": p.get("event_end_ts"),
            "price_to_beat": p.get("price_to_beat"),
            "price_to_beat_ts": p.get("price_to_beat_ts"),
            "price_to_beat_source": p.get("price_to_beat_source"),
            "price_to_beat_lag_ms": p.get("price_to_beat_lag_ms"),
            "raw_reference_event_id": p.get("raw_reference_event_id"),
            "status": p.get("status"),
            "final_reference_price": p.get("final_reference_price"),
            "final_reference_price_ts": p.get("final_reference_price_ts"),
            "final_reference_lag_ms": p.get("final_reference_lag_ms"),
            "direction_vs_price_to_beat": p.get("direction_vs_price_to_beat"),
            "raw_json": payload_raw_json(p),
        }
    ]
