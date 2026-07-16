"""btc_ticks and clock_sync table rows (M2B.3)."""

from __future__ import annotations

from typing import Any

from tyrex_pm.core.events import EventType, MarketEvent

from research.normalize.io import iso_ts, payload_raw_json


def btc_tick_rows_from_event(date: str, event: MarketEvent) -> list[dict[str, Any]]:
    if event.event_type != EventType.EXTERNAL_BTC_TICK:
        return []
    p = event.payload
    return [
        {
            "date": date,
            "source": p.get("source"),
            "symbol": p.get("symbol"),
            "stream": p.get("stream"),
            "event_id": event.event_id,
            "recv_ts": iso_ts(event.recv_ts),
            "source_ts": iso_ts(event.source_ts) or p.get("source_event_ts"),
            "bid": p.get("bid"),
            "ask": p.get("ask"),
            "mid": p.get("mid"),
            "price": p.get("price"),
            "quantity": p.get("quantity"),
            "trade_id": p.get("trade_id"),
            "raw_json": payload_raw_json(p),
        }
    ]


def clock_sync_rows_from_event(date: str, event: MarketEvent) -> list[dict[str, Any]]:
    if event.event_type != EventType.CLOCK_SYNC:
        return []
    p = event.payload
    return [
        {
            "date": date,
            "source": p.get("source"),
            "symbol": p.get("symbol"),
            "event_id": event.event_id,
            "recv_ts": iso_ts(event.recv_ts),
            "source_ts": p.get("source_ts") or iso_ts(event.source_ts),
            "latency_ms": p.get("latency_ms"),
            "raw_json": payload_raw_json(p),
        }
    ]
