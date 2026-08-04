"""Thin Tyrex boundary over official ``polymarket`` Public / AsyncPublic clients.

SDK models stay inside this module. Callers receive Tyrex RestBookPayload /
book events only.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Callable, Mapping, Protocol, Sequence

from tyrex_pm.adapters.polymarket.sdk_errors import classify_polymarket_error, raise_as_tyrex
from tyrex_pm.core.book_events import (
    BookDeltaReceived,
    BookLevelDelta,
    BookSide,
    BookSnapshotReceived,
    TickSizeChanged,
)
from tyrex_pm.core.events import EventSource
from tyrex_pm.core.ids import (
    CorrelationId,
    InstrumentId,
    MarketId,
    new_correlation_id,
    new_event_id,
)
from tyrex_pm.core.snapshots import BookLevel, BookSnapshot

logger = logging.getLogger(__name__)

DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BASE_S = 0.25


class RestBookPayload:
    """Imported lazily-compatible dataclass shape — defined in rest_book."""

    pass


def _ms_to_utc(value: Any) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    if ms < 10_000_000_000:
        ms *= 1000
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _levels_best_first(
    levels: Sequence[Any],
    *,
    side: str,
) -> tuple[BookLevel, ...]:
    """SDK OrderBook: bids ascending (best last), asks descending (best last).

    Tyrex BookSnapshot expects bids descending / asks ascending (best first).
    """
    parsed: list[BookLevel] = []
    for row in levels or ():
        price = getattr(row, "price", None)
        size = getattr(row, "size", None)
        if price is None or size is None:
            continue
        price_d = price if isinstance(price, Decimal) else Decimal(str(price))
        size_d = size if isinstance(size, Decimal) else Decimal(str(size))
        if size_d < 0:
            continue
        parsed.append(BookLevel(price=price_d, quantity=size_d))
    if side == "BID":
        parsed.sort(key=lambda lv: lv.price, reverse=True)
    else:
        parsed.sort(key=lambda lv: lv.price)
    seen: set[Decimal] = set()
    out: list[BookLevel] = []
    for lv in parsed:
        if lv.price in seen:
            continue
        seen.add(lv.price)
        out.append(lv)
    return tuple(out)


def order_book_to_rest_payload(
    order_book: Any,
    *,
    expected_token_id: str | None = None,
) -> Any:
    """Convert SDK ``OrderBook`` → ``RestBookPayload`` (Tyrex decimals, best-first)."""
    from tyrex_pm.adapters.polymarket.rest_book import RestBookPayload

    token_id = str(getattr(order_book, "token_id", "") or "")
    if not token_id:
        raise ValueError("sdk_order_book_missing_token_id")
    if expected_token_id is not None and token_id != str(expected_token_id):
        raise ValueError(f"sdk_token_mismatch:expected={expected_token_id}:got={token_id}")

    ts = _ms_to_utc(getattr(order_book, "timestamp", None))
    bids = _levels_best_first(getattr(order_book, "bids", ()) or (), side="BID")
    asks = _levels_best_first(getattr(order_book, "asks", ()) or (), side="ASK")
    try:
        book = BookSnapshot(
            instrument_id=InstrumentId(token_id),
            ts_event=ts,
            bids=bids,
            asks=asks,
        )
    except ValueError as exc:
        raise ValueError(f"rest_book_invalid:{exc}") from exc

    tick = getattr(order_book, "tick_size", None)
    min_sz = getattr(order_book, "min_order_size", None)
    venue_hash = getattr(order_book, "hash", None)
    return RestBookPayload(
        token_id=token_id,
        book=book,
        venue_hash=None if venue_hash is None else str(venue_hash),
        tick_size=None if tick is None else Decimal(str(tick)),
        min_order_size=None if min_sz is None else Decimal(str(min_sz)),
        source_ts=ts,
        raw={
            "token_id": token_id,
            "condition_id": str(getattr(order_book, "condition_id", "") or ""),
            "hash": venue_hash,
            "tick_size": str(tick) if tick is not None else None,
            "min_order_size": str(min_sz) if min_sz is not None else None,
            "source": "polymarket.PublicClient.get_order_book",
        },
    )


def normalize_rest_dict_payload(
    payload: Mapping[str, Any],
    *,
    token_id: str | None = None,
) -> Any:
    """Normalize a plain dict (tests / fixtures) into RestBookPayload."""
    from tyrex_pm.adapters.polymarket.rest_book import normalize_rest_book_payload

    return normalize_rest_book_payload(payload, token_id=token_id)


def sdk_market_event_to_tyrex(
    event: Any,
    *,
    ts_received: datetime | None = None,
    correlation_id: CorrelationId | None = None,
    market_id: MarketId | None = None,
) -> BookSnapshotReceived | BookDeltaReceived | TickSizeChanged | None:
    """Translate official SDK market WS events into Tyrex book contracts."""
    received = ts_received or datetime.now(timezone.utc)
    etype = getattr(event, "type", None) or getattr(event, "event_type", None)
    if etype is None and isinstance(event, Mapping):
        return None
    etype_s = str(etype).lower()

    if etype_s == "book":
        payload = getattr(event, "payload", event)
        token_id = str(getattr(payload, "token_id", "") or "")
        if not token_id:
            raise ValueError("sdk_book_missing_token_id")
        ts_event = _ms_to_utc(getattr(payload, "timestamp", None))
        bids = _levels_best_first(getattr(payload, "bids", ()) or (), side="BID")
        asks = _levels_best_first(getattr(payload, "asks", ()) or (), side="ASK")
        book = BookSnapshot(
            instrument_id=InstrumentId(token_id),
            ts_event=ts_event,
            bids=bids,
            asks=asks,
        )
        venue_hash = getattr(payload, "hash", None)
        return BookSnapshotReceived(
            event_id=new_event_id(),
            correlation_id=correlation_id or new_correlation_id(),
            causation_id=None,
            ts_event=ts_event,
            ts_received=received,
            source=EventSource.POLYMARKET_CLOB,
            book=book,
            market_id=market_id,
            venue_hash=None if venue_hash is None else str(venue_hash),
        )

    if etype_s == "price_change":
        payload = getattr(event, "payload", event)
        ts_event = _ms_to_utc(getattr(payload, "timestamp", None))
        changes: list[BookLevelDelta] = []
        for row in getattr(payload, "price_changes", ()) or ():
            asset_id = str(getattr(row, "token_id", "") or "")
            if not asset_id:
                continue
            side_raw = str(getattr(row, "side", "") or "").upper()
            if side_raw in ("BUY", "BID", "B"):
                side = BookSide.BID
            elif side_raw in ("SELL", "ASK", "A"):
                side = BookSide.ASK
            else:
                raise ValueError(f"unknown book side: {side_raw!r}")
            price = getattr(row, "price")
            size = getattr(row, "size")
            changes.append(
                BookLevelDelta(
                    instrument_id=InstrumentId(asset_id),
                    side=side,
                    price=price if isinstance(price, Decimal) else Decimal(str(price)),
                    size=size if isinstance(size, Decimal) else Decimal(str(size)),
                )
            )
        if not changes:
            raise ValueError("sdk_price_change_empty")
        return BookDeltaReceived(
            event_id=new_event_id(),
            correlation_id=correlation_id or new_correlation_id(),
            causation_id=None,
            ts_event=ts_event,
            ts_received=received,
            source=EventSource.POLYMARKET_CLOB,
            changes=tuple(changes),
            market_id=market_id,
        )

    if etype_s == "tick_size_change":
        payload = getattr(event, "payload", event)
        token_id = str(
            getattr(payload, "token_id", None)
            or getattr(payload, "asset_id", None)
            or ""
        )
        if not token_id:
            raise ValueError("sdk_tick_size_change_missing_token_id")
        ts_event = _ms_to_utc(getattr(payload, "timestamp", None))
        old_tick = getattr(payload, "old_tick_size", None) or getattr(
            payload, "oldTickSize", "0"
        )
        new_tick = getattr(payload, "new_tick_size", None) or getattr(
            payload, "newTickSize", None
        )
        if new_tick is None:
            raise ValueError("sdk_tick_size_change_missing_new_tick")
        return TickSizeChanged(
            event_id=new_event_id(),
            correlation_id=correlation_id or new_correlation_id(),
            causation_id=None,
            ts_event=ts_event,
            ts_received=received,
            source=EventSource.POLYMARKET_CLOB,
            instrument_id=InstrumentId(token_id),
            old_tick_size=Decimal(str(old_tick)),
            new_tick_size=Decimal(str(new_tick)),
            market_id=market_id,
        )

    # Unsupported / ignored market event types (last_trade_price, best_bid_ask, …)
    return None


class PublicBookPort(Protocol):
    def get_order_book(self, token_id: str) -> Any: ...


def build_public_client() -> Any:
    from polymarket import PRODUCTION, PublicClient

    return PublicClient(environment=PRODUCTION)


def build_async_public_client() -> Any:
    from polymarket import PRODUCTION, AsyncPublicClient

    return AsyncPublicClient(environment=PRODUCTION)


def fetch_order_book_via_sdk(
    token_id: str,
    *,
    client: Any | None = None,
    retries: int = DEFAULT_RETRY_ATTEMPTS,
    retry_base_s: float = DEFAULT_RETRY_BASE_S,
) -> Any:
    """Public REST order book via official PublicClient (no urllib)."""
    owns = client is None
    pub = client or build_public_client()
    last_exc: BaseException | None = None
    try:
        for attempt in range(max(1, retries)):
            try:
                order_book = pub.get_order_book(token_id=str(token_id))
                return order_book_to_rest_payload(order_book, expected_token_id=str(token_id))
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                classified = classify_polymarket_error(exc)
                if not classified.retryable or attempt >= retries - 1:
                    raise_as_tyrex(exc, prefix="rest_book")
                sleep_s = retry_base_s * (2**attempt)
                logger.warning(
                    "REST book retry token=%s attempt=%s category=%s",
                    token_id,
                    attempt + 1,
                    classified.category.value,
                )
                time.sleep(sleep_s)
        assert last_exc is not None
        raise_as_tyrex(last_exc, prefix="rest_book")
    finally:
        if owns and hasattr(pub, "close"):
            try:
                pub.close()
            except Exception:  # noqa: BLE001
                pass


async def subscribe_market_books(
    token_ids: Sequence[str],
    *,
    client: Any | None = None,
) -> tuple[Any, AsyncIterator[Any]]:
    """Subscribe via AsyncPublicClient; returns (client_or_handle_owner, event iterator)."""
    from polymarket.streams._specs import MarketSpec

    owns = client is None
    pub = client or build_async_public_client()
    handle = await pub.subscribe(MarketSpec(token_ids=list(token_ids)))
    return (pub if owns else None), handle


MarketStreamFactory = Callable[[Sequence[str]], Any]
