"""Public CLOB order-book bootstrap / recovery via official polymarket-client."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping

from tyrex_pm.adapters.polymarket.sdk_public import fetch_order_book_via_sdk
from tyrex_pm.core.ids import InstrumentId
from tyrex_pm.core.snapshots import BookLevel, BookSnapshot
from tyrex_pm.market_data.book_store import MarketStateStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class RestBookPayload:
    token_id: str
    book: BookSnapshot
    venue_hash: str | None
    tick_size: Decimal | None
    min_order_size: Decimal | None
    source_ts: datetime
    raw: Mapping[str, Any]


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


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


def _levels_best_first(raw: Any, *, side: str) -> tuple[BookLevel, ...]:
    """Normalize dict levels to internal best-first order (tests / fixtures)."""
    if not isinstance(raw, list):
        return ()
    parsed: list[BookLevel] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        price = _dec(row.get("price") if "price" in row else row.get("p"))
        size = _dec(row.get("size") if "size" in row else row.get("s"))
        if price is None or size is None:
            continue
        if size < 0:
            continue
        parsed.append(BookLevel(price=price, quantity=size))
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


def normalize_rest_book_payload(
    payload: Mapping[str, Any],
    *,
    token_id: str | None = None,
) -> RestBookPayload:
    asset = str(
        token_id
        or payload.get("asset_id")
        or payload.get("assetId")
        or payload.get("token_id")
        or ""
    )
    if not asset:
        raise ValueError("REST book missing asset_id/token_id")
    ts = _ms_to_utc(payload.get("timestamp") or payload.get("ts"))
    bids = _levels_best_first(payload.get("bids") or payload.get("buys"), side="BID")
    asks = _levels_best_first(payload.get("asks") or payload.get("sells"), side="ASK")
    try:
        book = BookSnapshot(
            instrument_id=InstrumentId(asset),
            ts_event=ts,
            bids=bids,
            asks=asks,
        )
    except ValueError as exc:
        raise ValueError(f"rest_book_invalid:{exc}") from exc
    return RestBookPayload(
        token_id=asset,
        book=book,
        venue_hash=None if payload.get("hash") is None else str(payload.get("hash")),
        tick_size=_dec(payload.get("tick_size") or payload.get("tickSize")),
        min_order_size=_dec(payload.get("min_order_size") or payload.get("minOrderSize")),
        source_ts=ts,
        raw=dict(payload),
    )


def fetch_clob_book(
    token_id: str,
    *,
    client: Any | None = None,
    timeout_s: float = 10.0,
) -> RestBookPayload:
    """Fetch public order book via official ``PublicClient.get_order_book``.

    ``timeout_s`` is retained for call-site compatibility; the SDK owns HTTP timeouts.
    """
    _ = timeout_s
    return fetch_order_book_via_sdk(token_id, client=client)


def bootstrap_token_into_store(
    store: MarketStateStore,
    token_id: str,
    *,
    binding_id: str | None = None,
    connection_epoch: int = 0,
    mark_ready: bool = False,
    fetcher: Callable[[str], RestBookPayload] = fetch_clob_book,
) -> RestBookPayload:
    """Fetch REST book and apply as bootstrap (SYNCING unless mark_ready)."""
    request_start = store.get(InstrumentId(token_id)).book_version
    payload = fetcher(token_id)
    if payload.token_id != str(token_id):
        raise RuntimeError(
            f"rest_token_mismatch:expected={token_id}:got={payload.token_id}"
        )
    ok = store.apply_rest_snapshot(
        payload.book,
        ts_received=datetime.now(timezone.utc),
        venue_hash=payload.venue_hash,
        tick_size=payload.tick_size,
        min_order_size=payload.min_order_size,
        binding_id=binding_id,
        connection_epoch=connection_epoch,
        request_start_version=request_start,
        mark_ready=mark_ready,
        source="REST_BOOTSTRAP",
    )
    if not ok:
        raise RuntimeError(f"rest_snapshot_rejected:{token_id}")
    return payload


__all__ = [
    "RestBookPayload",
    "normalize_rest_book_payload",
    "fetch_clob_book",
    "bootstrap_token_into_store",
]
