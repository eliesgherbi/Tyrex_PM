"""REST order-book snapshot → :class:`MarketStateStore` bootstrap/repair (Phase 2).

The market WebSocket is the primary live source; this REST path seeds the store
at startup and repairs it after a reconnect. It reuses the existing pure book
parser from ``strategies.sell_test.pricing`` (``best_levels_from_book``) and the
threaded ``fetch_order_book`` SDK wrapper so it never blocks the event loop.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from tyrex_pm.core.ids import TokenId
from tyrex_pm.state.market_store import MarketBookSnapshot, MarketStateStore, make_snapshot
from tyrex_pm.strategies.sell_test.pricing import fetch_order_book

log = logging.getLogger(__name__)


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


def book_payload_to_snapshot(token_id: TokenId, book: Any) -> MarketBookSnapshot:
    """Convert a Polymarket REST book payload (``{"bids":[...], "asks":[...]}``)."""
    bids = _levels(book.get("bids")) if isinstance(book, dict) else []
    asks = _levels(book.get("asks")) if isinstance(book, dict) else []
    return make_snapshot(token_id, bids=bids, asks=asks)


async def bootstrap_market_store_from_rest(
    store: MarketStateStore,
    client: Any,
    token_ids: Iterable[str | TokenId],
) -> int:
    """Fetch each token's REST book and apply it to ``store``.

    Returns the number of tokens successfully bootstrapped. Per-token failures
    are logged and skipped (fail-soft) so one bad token does not strand the rest.
    """
    applied = 0
    for raw_tid in token_ids:
        tid = TokenId(str(raw_tid))
        try:
            book = await fetch_order_book(client, str(tid))
        except Exception as e:  # noqa: BLE001 — fail-soft per token
            log.warning("market_store REST bootstrap: get_order_book(%s) failed: %r", tid, e)
            continue
        store.apply_snapshot(book_payload_to_snapshot(tid, book))
        applied += 1
    return applied
