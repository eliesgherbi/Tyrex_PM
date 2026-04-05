"""C3: best bid/ask snapshot from Cache L2 or optional REST order book."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nautilus_trader.model.identifiers import InstrumentId

@dataclass(frozen=True, slots=True)
class BookTop:
    """Top-of-book summary for C3 guard / depth MVP."""

    best_bid: float | None
    best_ask: float | None
    best_bid_size: float | None
    best_ask_size: float | None
    source: str  # "cache" | "rest" | "none"


def _float_px(px: Any) -> float | None:
    if px is None:
        return None
    try:
        return float(px)
    except (TypeError, ValueError):
        try:
            return float(px.as_double())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return None


def _float_sz(sz: Any) -> float | None:
    if sz is None:
        return None
    try:
        return float(sz)
    except (TypeError, ValueError):
        try:
            return float(sz.as_double())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return None


def book_top_from_cache(cache: Any, instrument_id: InstrumentId) -> BookTop | None:
    if not cache.has_order_book(instrument_id):
        return None
    book = cache.order_book(instrument_id)
    bid_p = _float_px(book.best_bid_price())
    ask_p = _float_px(book.best_ask_price())
    bid_s = _float_sz(book.best_bid_size())
    ask_s = _float_sz(book.best_ask_size())
    if bid_p is None and ask_p is None:
        return None
    return BookTop(
        best_bid=bid_p,
        best_ask=ask_p,
        best_bid_size=bid_s,
        best_ask_size=ask_s,
        source="cache",
    )


def book_top_from_rest(*, token_id: str, clob: Any) -> BookTop | None:
    """``clob`` is ``ClobClient`` with ``get_order_book``."""
    try:
        raw = clob.get_order_book(token_id)
    except Exception:  # noqa: BLE001
        return None
    bids = getattr(raw, "bids", None) or []
    asks = getattr(raw, "asks", None) or []
    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    best_bid_sz = float(bids[0]["size"]) if bids else None
    best_ask_sz = float(asks[0]["size"]) if asks else None
    if best_bid is None and best_ask is None:
        return BookTop(None, None, None, None, "none")
    return BookTop(
        best_bid=best_bid,
        best_ask=best_ask,
        best_bid_size=best_bid_sz,
        best_ask_size=best_ask_sz,
        source="rest",
    )


def resolve_book_top(
    *,
    cache: Any,
    instrument_id: InstrumentId,
    token_id: str,
    rest_enabled: bool,
    clob: Any | None,
) -> BookTop:
    hit = book_top_from_cache(cache, instrument_id)
    if hit is not None:
        return hit
    if rest_enabled and clob is not None:
        rest = book_top_from_rest(token_id=token_id, clob=clob)
        if rest is not None:
            return rest
    return BookTop(None, None, None, None, "none")
