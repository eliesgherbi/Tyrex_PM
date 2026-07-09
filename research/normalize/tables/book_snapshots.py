"""book_snapshots table rows (M2B.3)."""

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


def _levels(raw: Any) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not isinstance(raw, list):
        return out
    for lv in raw:
        if not isinstance(lv, dict):
            continue
        price = lv.get("price")
        size = lv.get("size", "0")
        if price is None:
            continue
        out.append((str(price), str(size)))
    return out


def rows_from_event(date: str, market_id: str, event: MarketEvent) -> list[dict[str, Any]]:
    if event.event_type != EventType.BOOK_SNAPSHOT:
        return []
    raw = event.payload.get("raw")
    if not isinstance(raw, dict):
        return []

    bids = _levels(raw.get("bids") or raw.get("buys"))
    asks = _levels(raw.get("asks") or raw.get("sells"))
    best_bid = _dec(bids[0][0]) if bids else None
    best_ask = _dec(asks[0][0]) if asks else None
    mid_s, spread_s = _mid_spread(best_bid, best_ask)
    book_hash = raw.get("hash")
    base = {
        "date": date,
        "market_id": market_id,
        "event_id": event.event_id,
        "recv_ts": iso_ts(event.recv_ts),
        "source_ts": iso_ts(event.source_ts),
        "token_id": str(event.token_id) if event.token_id is not None else raw.get("asset_id"),
        "best_bid": str(best_bid) if best_bid is not None else None,
        "best_ask": str(best_ask) if best_ask is not None else None,
        "mid": mid_s,
        "spread": spread_s,
        "book_hash": str(book_hash) if book_hash is not None else None,
        "venue_cursor": event.venue_cursor,
        "raw_json": payload_raw_json(event.payload),
    }
    rows: list[dict[str, Any]] = []
    for price, size in bids:
        rows.append(
            {
                **base,
                "side": "BUY",
                "bid_price": price,
                "bid_size": size,
                "ask_price": None,
                "ask_size": None,
            }
        )
    for price, size in asks:
        rows.append(
            {
                **base,
                "side": "SELL",
                "bid_price": None,
                "bid_size": None,
                "ask_price": price,
                "ask_size": size,
            }
        )
    if not rows:
        rows.append(
            {
                **base,
                "side": None,
                "bid_price": None,
                "bid_size": None,
                "ask_price": None,
                "ask_size": None,
            }
        )
    return rows
