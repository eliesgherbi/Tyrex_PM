"""Market data ingestion → :class:`MarketStateStore` (Phase 2 architecture_enhance).

Single-writer: these helpers are the only live producers of book snapshots for
the store (REST bootstrap is the other, in ``venue/polymarket/book_snapshot.py``).
They parse Polymarket market-channel payloads (``book`` full snapshots and
``price_change`` deltas) and apply them via :meth:`MarketStateStore.apply_book`.

Dark-launch (Phase 2): nothing wires a live market WebSocket by default; the
store stays empty and every read is reported stale. Shadow WS (M1) writes a
**separate** ``MarketStateStoreShadow`` instance only.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.market_data.models import BookLevel, BookSource
from tyrex_pm.state.market_store import MarketBookSnapshot, MarketStateStore, parse_exchange_ts


def _dec(x: Any) -> Decimal | None:
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _levels(raw: Any) -> list[tuple[Decimal, Decimal]]:
    out: list[tuple[Decimal, Decimal]] = []
    if not isinstance(raw, list):
        return out
    for lv in raw:
        if not isinstance(lv, dict):
            continue
        price = _dec(lv.get("price"))
        size = _dec(lv.get("size", "0"))
        if price is None or size is None:
            continue
        out.append((price, size))
    return out


def parse_book_message(msg: dict[str, Any]) -> tuple[TokenId, list[BookLevel], list[BookLevel], dict[str, Any]] | None:
    """Parse a market-channel ``book`` full-snapshot payload."""
    asset = msg.get("asset_id") or msg.get("market")
    if not asset:
        return None
    bids = [BookLevel(p, s) for p, s in _levels(msg.get("bids") or msg.get("buys"))]
    asks = [BookLevel(p, s) for p, s in _levels(msg.get("asks") or msg.get("sells"))]
    meta = {
        "exchange_ts": parse_exchange_ts(msg.get("timestamp")),
        "book_hash": str(msg["hash"]) if msg.get("hash") is not None else None,
    }
    return TokenId(str(asset)), bids, asks, meta


def _apply_changes_to_side(
    levels: tuple[BookLevel, ...],
    changes: dict[Decimal, Decimal],
) -> list[BookLevel]:
    merged: dict[Decimal, Decimal] = {lv.price: lv.size for lv in levels}
    for price, size in changes.items():
        if size <= 0:
            merged.pop(price, None)
        else:
            merged[price] = size
    return [BookLevel(p, s) for p, s in merged.items()]


def apply_price_change(store: MarketStateStore, msg: dict[str, Any], *, source: str = BookSource.WEBSOCKET) -> bool:
    """Apply a ``price_change`` delta on top of the current book for the token.

    Supports legacy ``changes`` + top-level ``asset_id`` and Polymarket
    ``price_changes[]`` with per-row ``asset_id``.
    """
    if msg.get("price_changes"):
        applied = False
        for ch in msg.get("price_changes") or []:
            if not isinstance(ch, dict):
                continue
            asset = ch.get("asset_id") or msg.get("asset_id") or msg.get("market")
            if not asset:
                continue
            side = str(ch.get("side", "")).upper()
            price = _dec(ch.get("price"))
            size = _dec(ch.get("size", "0"))
            if price is None or size is None or side not in {"BUY", "SELL"}:
                continue
            delta = {
                "event_type": "price_change",
                "asset_id": str(asset),
                "changes": [{"price": str(price), "size": str(size), "side": side}],
                "timestamp": msg.get("timestamp"),
            }
            if ch.get("hash") is not None:
                delta["hash"] = ch.get("hash")
            applied = apply_price_change(store, delta, source=source) or applied
        return applied

    asset = msg.get("asset_id") or msg.get("market")
    if not asset:
        return False
    token_id = TokenId(str(asset))
    current = store.snapshot(token_id)
    bid_changes: dict[Decimal, Decimal] = {}
    ask_changes: dict[Decimal, Decimal] = {}
    for ch in msg.get("changes") or []:
        if not isinstance(ch, dict):
            continue
        price = _dec(ch.get("price"))
        size = _dec(ch.get("size", "0"))
        if price is None or size is None:
            continue
        side = str(ch.get("side", "")).upper()
        if side == "BUY":
            bid_changes[price] = size
        elif side == "SELL":
            ask_changes[price] = size
    if not bid_changes and not ask_changes:
        return False
    bids = _apply_changes_to_side(current.bids if current else (), bid_changes)
    asks = _apply_changes_to_side(current.asks if current else (), ask_changes)
    store.apply_book(
        token_id,
        bids,
        asks,
        source=source,
        received_ts=utc_now(),
        exchange_ts=parse_exchange_ts(msg.get("timestamp")),
        book_hash=str(msg["hash"]) if msg.get("hash") is not None else None,
    )
    return True


def apply_book_message(
    store: MarketStateStore,
    msg: dict[str, Any],
    *,
    source: str = BookSource.WEBSOCKET,
) -> bool:
    parsed = parse_book_message(msg)
    if parsed is None:
        return False
    token_id, bids, asks, meta = parsed
    store.apply_book(
        token_id,
        bids,
        asks,
        source=source,
        received_ts=utc_now(),
        exchange_ts=meta.get("exchange_ts"),
        book_hash=meta.get("book_hash"),
    )
    return True


def apply_market_message(
    store: MarketStateStore,
    msg: dict[str, Any],
    *,
    source: str = BookSource.WEBSOCKET,
) -> bool:
    """Dispatch one market-channel payload to the store. Returns True if applied."""
    if not isinstance(msg, dict):
        return False
    event = str(msg.get("event_type") or msg.get("type") or "").lower()
    if event == "book":
        return apply_book_message(store, msg, source=source)
    if event == "price_change":
        return apply_price_change(store, msg, source=source)
    return False


def make_legacy_snapshot(
    token_id: TokenId,
    *,
    bids: list[tuple[Decimal, Decimal]] | None = None,
    asks: list[tuple[Decimal, Decimal]] | None = None,
    ts=None,
) -> MarketBookSnapshot:
    """Backward-compat helper used by tests — prefer ``apply_book`` in new code."""
    from tyrex_pm.state.market_store import make_snapshot

    return make_snapshot(token_id, bids=bids, asks=asks, ts=ts)
