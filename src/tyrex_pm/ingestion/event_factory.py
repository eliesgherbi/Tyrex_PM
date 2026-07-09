"""WS payload → :class:`~tyrex_pm.core.events.MarketEvent` (Phase 2B M2B.0-B / M2B.3-A)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from tyrex_pm.core.events import (
    EventType,
    MarketEvent,
    compute_event_id,
    compute_payload_digest,
)
from tyrex_pm.core.ids import TokenId
from tyrex_pm.state.market_store import parse_exchange_ts

__all__ = ["build_market_event_from_ws", "WS_EVENT_TYPE_MAP"]

_WS_EVENT_MAP: dict[str, EventType] = {
    "book": EventType.BOOK_SNAPSHOT,
    "price_change": EventType.BOOK_DELTA,
    "last_trade_price": EventType.LAST_TRADE_PRICE,
    "tick_size_change": EventType.TICK_SIZE_CHANGE,
    "best_bid_ask": EventType.BEST_BID_ASK,
    "new_market": EventType.NEW_MARKET,
    "market_resolved": EventType.MARKET_RESOLVED,
}

WS_EVENT_TYPE_MAP = dict(_WS_EVENT_MAP)


def _extract_token_id(msg: dict[str, Any]) -> TokenId | None:
    asset = msg.get("asset_id")
    if asset is None and msg.get("price_changes"):
        pcs = msg.get("price_changes") or []
        if pcs and isinstance(pcs[0], dict):
            asset = pcs[0].get("asset_id")
    if asset is None:
        asset = msg.get("market")
    return TokenId(str(asset)) if asset else None


def _extract_venue_cursor(msg: dict[str, Any], event_name: str) -> str | None:
    for key in ("hash", "id", "timestamp", "transaction_hash"):
        val = msg.get(key)
        if val is not None and str(val).strip():
            return str(val)
    if event_name == "price_change":
        pcs = msg.get("price_changes") or []
        if pcs and isinstance(pcs[0], dict) and pcs[0].get("hash"):
            return str(pcs[0]["hash"])
    return None


def build_market_event_from_ws(
    msg: dict[str, Any],
    *,
    received_ts: datetime,
    connection_id: str,
    local_counter: int,
    market_id: str | None = None,
) -> MarketEvent | None:
    """Map WS market-channel payloads to canonical :class:`MarketEvent`.

    ``payload["raw"]`` retains the full WS message. Unknown event types return
    ``None`` (caller may count/skip without stopping the recorder).
    """
    if not isinstance(msg, dict):
        return None
    event_name = str(msg.get("event_type") or msg.get("type") or "").lower()
    event_type = _WS_EVENT_MAP.get(event_name)
    if event_type is None:
        return None

    token_id = _extract_token_id(msg)
    venue_cursor = _extract_venue_cursor(msg, event_name)
    source_ts = parse_exchange_ts(msg.get("timestamp"))
    payload: dict[str, Any] = {"raw": dict(msg)}
    digest = compute_payload_digest(payload)
    event_id = compute_event_id(
        event_type=event_type.value,
        token_id=str(token_id) if token_id is not None else None,
        venue_cursor=venue_cursor,
        source_ts=source_ts,
        payload_digest=digest,
    )
    return MarketEvent(
        event_id=event_id,
        event_type=event_type,
        market_id=market_id,
        token_id=token_id,
        venue_cursor=venue_cursor,
        source_ts=source_ts,
        recv_ts=received_ts,
        payload=payload,
        connection_id=connection_id,
        local_counter=local_counter,
    )
