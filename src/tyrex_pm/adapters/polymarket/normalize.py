"""Normalize Polymarket CLOB market-channel payloads to core events.

Protocol evidence (Option B — snapshot + delta):
- Official docs: market channel ``wss://ws-subscriptions-clob.polymarket.com/ws/market``
  emits ``book`` (full snapshot), ``price_change`` (level deltas; size ``\"0\"`` removes),
  and ``tick_size_change``.
- Legacy ``old/src/tyrex_pm/venue/polymarket/market_ws.py`` is a stub and not used.
- Store (not adapter) owns reconstructed books.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from tyrex_pm.core.book_events import (
    BookDeltaReceived,
    BookLevelDelta,
    BookSide,
    BookSnapshotReceived,
    TickSizeChanged,
)
from tyrex_pm.core.events import EventSource
from tyrex_pm.core.ids import CorrelationId, InstrumentId, MarketId, new_correlation_id, new_event_id
from tyrex_pm.core.snapshots import BookLevel, BookSnapshot


def _ms_to_utc(value: Any) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).astimezone(timezone.utc)
        except ValueError:
            value = int(value)
    ms = int(value)
    # Accept seconds if clearly small
    if ms < 10_000_000_000:
        ms *= 1000
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _levels(raw: list[Mapping[str, Any]] | None) -> tuple[BookLevel, ...]:
    if not raw:
        return ()
    out: list[BookLevel] = []
    for row in raw:
        price = row.get("price", row.get("p"))
        size = row.get("size", row.get("s", row.get("quantity")))
        if price is None or size is None:
            continue
        out.append(BookLevel(price=Decimal(str(price)), quantity=Decimal(str(size))))
    return tuple(out)


def normalize_book_message(
    payload: Mapping[str, Any],
    *,
    ts_received: datetime,
    correlation_id: CorrelationId | None = None,
    market_id: MarketId | None = None,
) -> BookSnapshotReceived:
    asset_id = str(payload.get("asset_id") or payload.get("assetId") or "")
    if not asset_id:
        raise ValueError("book message missing asset_id")
    instrument_id = InstrumentId(asset_id)
    ts_event = _ms_to_utc(payload.get("timestamp") or payload.get("ts"))
    book = BookSnapshot(
        instrument_id=instrument_id,
        ts_event=ts_event,
        bids=_levels(payload.get("bids") or payload.get("buys")),
        asks=_levels(payload.get("asks") or payload.get("sells")),
    )
    return BookSnapshotReceived(
        event_id=new_event_id(),
        correlation_id=correlation_id or new_correlation_id(),
        causation_id=None,
        ts_event=ts_event,
        ts_received=ts_received,
        source=EventSource.POLYMARKET_CLOB,
        book=book,
        market_id=market_id,
        venue_hash=str(payload["hash"]) if payload.get("hash") is not None else None,
    )


def normalize_price_change(
    payload: Mapping[str, Any],
    *,
    ts_received: datetime,
    correlation_id: CorrelationId | None = None,
    market_id: MarketId | None = None,
) -> BookDeltaReceived:
    """Normalize ``price_change`` / ``price_changes`` payloads."""
    ts_event = _ms_to_utc(payload.get("timestamp") or payload.get("ts"))
    changes_raw = payload.get("price_changes") or payload.get("changes") or []
    # Some payloads wrap a single change at top level
    if not changes_raw and payload.get("asset_id") and payload.get("price") is not None:
        changes_raw = [payload]

    changes: list[BookLevelDelta] = []
    for row in changes_raw:
        asset_id = str(row.get("asset_id") or row.get("assetId") or "")
        if not asset_id:
            continue
        side_raw = str(row.get("side") or row.get("book_side") or "").upper()
        if side_raw in ("BUY", "BID", "B"):
            side = BookSide.BID
        elif side_raw in ("SELL", "ASK", "A"):
            side = BookSide.ASK
        else:
            raise ValueError(f"unknown book side: {side_raw!r}")
        changes.append(
            BookLevelDelta(
                instrument_id=InstrumentId(asset_id),
                side=side,
                price=Decimal(str(row["price"])),
                size=Decimal(str(row.get("size", row.get("quantity", "0")))),
            )
        )
    if not changes:
        raise ValueError("price_change produced no level deltas")
    return BookDeltaReceived(
        event_id=new_event_id(),
        correlation_id=correlation_id or new_correlation_id(),
        causation_id=None,
        ts_event=ts_event,
        ts_received=ts_received,
        source=EventSource.POLYMARKET_CLOB,
        changes=tuple(changes),
        market_id=market_id,
    )


def normalize_tick_size_change(
    payload: Mapping[str, Any],
    *,
    ts_received: datetime,
    correlation_id: CorrelationId | None = None,
    market_id: MarketId | None = None,
) -> TickSizeChanged:
    asset_id = str(payload.get("asset_id") or payload.get("assetId") or "")
    if not asset_id:
        raise ValueError("tick_size_change missing asset_id")
    ts_event = _ms_to_utc(payload.get("timestamp") or payload.get("ts"))
    return TickSizeChanged(
        event_id=new_event_id(),
        correlation_id=correlation_id or new_correlation_id(),
        causation_id=None,
        ts_event=ts_event,
        ts_received=ts_received,
        source=EventSource.POLYMARKET_CLOB,
        instrument_id=InstrumentId(asset_id),
        old_tick_size=Decimal(str(payload.get("old_tick_size", payload.get("oldTickSize", "0")))),
        new_tick_size=Decimal(str(payload.get("new_tick_size") or payload.get("newTickSize"))),
        market_id=market_id,
    )


def normalize_market_ws_message(
    payload: Mapping[str, Any],
    *,
    ts_received: datetime,
    correlation_id: CorrelationId | None = None,
    market_id: MarketId | None = None,
) -> BookSnapshotReceived | BookDeltaReceived | TickSizeChanged | None:
    event_type = str(payload.get("event_type") or payload.get("type") or "").lower()
    if event_type == "book":
        return normalize_book_message(
            payload, ts_received=ts_received, correlation_id=correlation_id, market_id=market_id
        )
    if event_type in ("price_change", "price_changes"):
        return normalize_price_change(
            payload, ts_received=ts_received, correlation_id=correlation_id, market_id=market_id
        )
    if event_type == "tick_size_change":
        return normalize_tick_size_change(
            payload, ts_received=ts_received, correlation_id=correlation_id, market_id=market_id
        )
    return None
