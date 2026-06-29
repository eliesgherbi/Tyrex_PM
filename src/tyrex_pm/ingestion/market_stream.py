"""Market data ingestion → :class:`MarketStateStore` (Phase 2 architecture_enhance).

Single-writer: these helpers are the only live producers of book snapshots for
the store (REST bootstrap is the other, in ``venue/polymarket/book_snapshot.py``).
They parse Polymarket market-channel payloads (``book`` full snapshots and
``price_change`` deltas) and apply them via :meth:`MarketStateStore.apply_snapshot`.

Dark-launch (Phase 2): nothing wires a live market WebSocket by default; the
store stays empty and every read is reported stale. Activation is a Phase 3
concern gated on ``market_data.enabled``.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.state.market_store import BookLevel, MarketBookSnapshot, MarketStateStore, make_snapshot


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


def parse_book_message(msg: dict[str, Any]) -> MarketBookSnapshot | None:
    """Parse a market-channel ``book`` full-snapshot payload."""
    asset = msg.get("asset_id") or msg.get("market")
    if not asset:
        return None
    return make_snapshot(
        TokenId(str(asset)),
        bids=_levels(msg.get("bids") or msg.get("buys")),
        asks=_levels(msg.get("asks") or msg.get("sells")),
        ts=utc_now(),
    )


def _apply_changes_to_side(
    levels: tuple[BookLevel, ...],
    changes: dict[Decimal, Decimal],
) -> list[tuple[Decimal, Decimal]]:
    merged: dict[Decimal, Decimal] = {lv.price: lv.size for lv in levels}
    for price, size in changes.items():
        if size <= 0:
            merged.pop(price, None)
        else:
            merged[price] = size
    return [(p, s) for p, s in merged.items()]


def apply_price_change(store: MarketStateStore, msg: dict[str, Any]) -> bool:
    """Apply a ``price_change`` delta on top of the current book for the token.

    Returns ``True`` if a snapshot was (re)written. A delta for a token with no
    prior book seeds a fresh book from the changes alone.
    """
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
    store.apply_snapshot(make_snapshot(token_id, bids=bids, asks=asks, ts=utc_now()))
    return True


def apply_market_message(store: MarketStateStore, msg: dict[str, Any]) -> bool:
    """Dispatch one market-channel payload to the store. Returns True if applied."""
    if not isinstance(msg, dict):
        return False
    event = str(msg.get("event_type") or msg.get("type") or "").lower()
    if event == "book":
        snap = parse_book_message(msg)
        if snap is None:
            return False
        store.apply_snapshot(snap)
        return True
    if event == "price_change":
        return apply_price_change(store, msg)
    return False
