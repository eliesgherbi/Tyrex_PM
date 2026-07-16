"""trade_prices table rows (M2B.3-A)."""

from __future__ import annotations

from typing import Any

from tyrex_pm.core.events import EventType, MarketEvent

from research.normalize.io import iso_ts, payload_raw_json


def rows_from_event(date: str, market_id: str, event: MarketEvent) -> list[dict[str, Any]]:
    if event.event_type != EventType.LAST_TRADE_PRICE:
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
            "price": str(raw.get("price")) if raw.get("price") is not None else None,
            "size": str(raw.get("size")) if raw.get("size") is not None else None,
            "side": raw.get("side"),
            "fee_rate_bps": str(raw.get("fee_rate_bps")) if raw.get("fee_rate_bps") is not None else None,
            "transaction_hash": raw.get("transaction_hash"),
            "raw_json": payload_raw_json(event.payload),
        }
    ]
