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


def _sdk_book_mapping(book: Any) -> dict[str, Any]:
    if isinstance(book, Mapping):
        return dict(book)

    def levels(rows: Any) -> list[dict[str, Any]]:
        parsed: list[dict[str, Any]] = []
        for row in rows or ():
            if isinstance(row, Mapping):
                parsed.append({"price": row.get("price"), "size": row.get("size")})
            else:
                parsed.append(
                    {
                        "price": getattr(row, "price", None),
                        "size": getattr(row, "size", None),
                    }
                )
        return parsed

    return {
        "token_id": getattr(book, "token_id", None),
        "condition_id": getattr(book, "condition_id", None),
        "timestamp": getattr(book, "timestamp", None),
        "bids": levels(getattr(book, "bids", ())),
        "asks": levels(getattr(book, "asks", ())),
        "min_order_size": getattr(book, "min_order_size", None),
        "tick_size": getattr(book, "tick_size", None),
        "hash": getattr(book, "hash", None),
    }


def _same_condition(left: str, right: str) -> bool:
    def norm(value: str) -> str:
        text = str(value).strip().lower()
        return text[2:] if text.startswith("0x") else text

    return norm(left) == norm(right)


def recovery_book_view(book: Any, *, condition_id: str, token_id: str):
    """Build a two-leg view from one historical ``get_order_book`` response.

    The requested token is the ``up`` leg even when it is the down outcome.
    The other leg is an empty placeholder so the view stays well-formed.
    A mismatched token or condition returns None.
    """
    from tyrex_pm.market_data.book_health import ConnectionHealth, SideLiquidity, SyncHealth
    from tyrex_pm.market_data.book_view import BookView, LegBookSnapshot
    from tyrex_pm.market_data.executable import ExecutableQuote, book_quote

    payload = _sdk_book_mapping(book)
    observed_token = str(payload.get("token_id") or "")
    if observed_token and observed_token != str(token_id):
        return None
    observed_condition = str(payload.get("condition_id") or "")
    if observed_condition and not _same_condition(observed_condition, condition_id):
        return None
    try:
        normalized = normalize_rest_book_payload(payload, token_id=str(token_id))
    except ValueError:
        return None
    captured_at = datetime.now(timezone.utc)
    age_ms = max(0, int((captured_at - normalized.source_ts).total_seconds() * 1000))
    quote = book_quote(normalized.book)

    def liquidity(price: Decimal | None) -> SideLiquidity:
        if price is None:
            return SideLiquidity.EXPLICITLY_EMPTY
        return SideLiquidity.AVAILABLE

    active = LegBookSnapshot(
        token_id=str(token_id),
        instrument_id=InstrumentId(str(token_id)),
        book=normalized.book,
        book_version=1,
        venue_hash=normalized.venue_hash,
        sync_health=SyncHealth.READY,
        connection_health=ConnectionHealth.LIVE,
        bid_liquidity=liquidity(quote.best_bid),
        ask_liquidity=liquidity(quote.best_ask),
        tick_size=normalized.tick_size,
        min_order_size=normalized.min_order_size,
        source_ts=normalized.source_ts,
        receive_ts=captured_at,
        apply_ts=captured_at,
        apply_mono_ns=None,
        data_age_ms=age_ms,
        quote=quote,
    )
    complement_id = f"{token_id}:complement"
    empty_quote = ExecutableQuote(
        best_bid=None,
        best_ask=None,
        mid=None,
        spread=None,
        bid_size_at_touch=Decimal("0"),
        ask_size_at_touch=Decimal("0"),
    )
    complement = LegBookSnapshot(
        token_id=complement_id,
        instrument_id=InstrumentId(complement_id),
        book=None,
        book_version=0,
        venue_hash=None,
        sync_health=SyncHealth.READY,
        connection_health=ConnectionHealth.LIVE,
        bid_liquidity=SideLiquidity.EXPLICITLY_EMPTY,
        ask_liquidity=SideLiquidity.EXPLICITLY_EMPTY,
        tick_size=None,
        min_order_size=None,
        source_ts=captured_at,
        receive_ts=captured_at,
        apply_ts=captured_at,
        apply_mono_ns=None,
        data_age_ms=0,
        quote=empty_quote,
    )
    return BookView(
        binding_id=f"recovery:{condition_id}:{token_id}",
        role_epoch=0,
        window_slug="",
        condition_id=str(condition_id),
        connection_epoch=0,
        captured_at=captured_at,
        pair_version=(1, 0),
        up=active,
        down=complement,
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
        raise RuntimeError(f"rest_token_mismatch:expected={token_id}:got={payload.token_id}")
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
    "recovery_book_view",
    "fetch_clob_book",
    "bootstrap_token_into_store",
]
