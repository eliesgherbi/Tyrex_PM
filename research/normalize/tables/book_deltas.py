"""book_deltas table rows (M2B.3)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from tyrex_pm.core.events import EventType, MarketEvent

from research.normalize.io import iso_ts, payload_raw_json


def _dec(x: Any) -> Decimal | None:
    if x is None:
        return None
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _mid_spread(best_bid: Decimal | None, best_ask: Decimal | None) -> tuple[str | None, str | None]:
    if best_bid is None or best_ask is None:
        return None, None
    mid = (best_bid + best_ask) / Decimal("2")
    return str(mid), str(best_ask - best_bid)


def rows_from_event(date: str, market_id: str, event: MarketEvent) -> list[dict[str, Any]]:
    if event.event_type != EventType.BOOK_DELTA:
        return []
    raw = event.payload.get("raw")
    if not isinstance(raw, dict):
        return []

    rows: list[dict[str, Any]] = []
    if raw.get("price_changes"):
        for ch in raw.get("price_changes") or []:
            if not isinstance(ch, dict):
                continue
            side = str(ch.get("side", "")).upper() or None
            best_bid = _dec(ch.get("best_bid"))
            best_ask = _dec(ch.get("best_ask"))
            mid_s, spread_s = _mid_spread(best_bid, best_ask)
            rows.append(
                {
                    "date": date,
                    "market_id": market_id,
                    "event_id": event.event_id,
                    "recv_ts": iso_ts(event.recv_ts),
                    "source_ts": iso_ts(event.source_ts),
                    "token_id": ch.get("asset_id") or (str(event.token_id) if event.token_id else None),
                    "side": side,
                    "price": str(ch.get("price")) if ch.get("price") is not None else None,
                    "size": str(ch.get("size")) if ch.get("size") is not None else None,
                    "best_bid": str(best_bid) if best_bid is not None else None,
                    "best_ask": str(best_ask) if best_ask is not None else None,
                    "mid": mid_s,
                    "spread": spread_s,
                    "book_hash": str(ch.get("hash") or raw.get("hash") or "") or None,
                    "venue_cursor": event.venue_cursor,
                    "change_type": "price_change",
                    "raw_json": payload_raw_json(event.payload),
                }
            )
        return rows

    for ch in raw.get("changes") or []:
        if not isinstance(ch, dict):
            continue
        side = str(ch.get("side", "")).upper() or None
        rows.append(
            {
                "date": date,
                "market_id": market_id,
                "event_id": event.event_id,
                "recv_ts": iso_ts(event.recv_ts),
                "source_ts": iso_ts(event.source_ts),
                "token_id": str(event.token_id) if event.token_id else raw.get("asset_id"),
                "side": side,
                "price": str(ch.get("price")) if ch.get("price") is not None else None,
                "size": str(ch.get("size")) if ch.get("size") is not None else None,
                "best_bid": None,
                "best_ask": None,
                "mid": None,
                "spread": None,
                "book_hash": str(raw.get("hash") or "") or None,
                "venue_cursor": event.venue_cursor,
                "change_type": "changes",
                "raw_json": payload_raw_json(event.payload),
            }
        )
    if not rows:
        rows.append(
            {
                "date": date,
                "market_id": market_id,
                "event_id": event.event_id,
                "recv_ts": iso_ts(event.recv_ts),
                "source_ts": iso_ts(event.source_ts),
                "token_id": str(event.token_id) if event.token_id else raw.get("asset_id"),
                "side": None,
                "price": None,
                "size": None,
                "best_bid": None,
                "best_ask": None,
                "mid": None,
                "spread": None,
                "book_hash": str(raw.get("hash") or "") or None,
                "venue_cursor": event.venue_cursor,
                "change_type": None,
                "raw_json": payload_raw_json(event.payload),
            }
        )
    return rows
