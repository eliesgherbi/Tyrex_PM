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
from tyrex_pm.core.time import utc_now
from tyrex_pm.ingestion.event_factory import build_market_event_from_ws
from tyrex_pm.ingestion.market_stream import project_event
from tyrex_pm.market_data.models import BookSource
from tyrex_pm.state.market_store import BookLevel, MarketBookSnapshot, MarketStateStore, make_snapshot
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


def _rest_book_to_ws_message(token_id: TokenId, book: Any) -> dict[str, Any]:
    """Shape a REST book payload like a WS ``book`` message for event projection."""
    bids_raw: list[dict[str, str]] = []
    asks_raw: list[dict[str, str]] = []
    if isinstance(book, dict):
        for lv in book.get("bids") or []:
            if isinstance(lv, dict):
                bids_raw.append({"price": str(lv.get("price")), "size": str(lv.get("size", "0"))})
        for lv in book.get("asks") or []:
            if isinstance(lv, dict):
                asks_raw.append({"price": str(lv.get("price")), "size": str(lv.get("size", "0"))})
    return {
        "event_type": "book",
        "asset_id": str(token_id),
        "bids": bids_raw,
        "asks": asks_raw,
    }


async def bootstrap_market_store_from_rest(
    store: MarketStateStore,
    client: Any,
    token_ids: Iterable[str | TokenId],
    *,
    source: str = BookSource.REST_BOOTSTRAP,
    event_backbone_enabled: bool = False,
    event_backbone_market_id: str | None = None,
    emit_rest_recovery: bool = True,
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
        snap = book_payload_to_snapshot(tid, book)
        if event_backbone_enabled:
            recv_ts = utc_now()
            raw_msg = _rest_book_to_ws_message(tid, book)
            market_event = build_market_event_from_ws(
                raw_msg,
                received_ts=recv_ts,
                connection_id="rest-recovery",
                local_counter=applied,
                market_id=event_backbone_market_id,
            )
            if market_event is not None:
                if emit_rest_recovery:
                    log.debug(
                        "REST recovery event projection for %s (REST_RECOVERY_USED deferred to recorder)",
                        tid,
                    )
                project_event(store, market_event, source=source)
        else:
            store.apply_book(
                tid,
                list(snap.bids),
                list(snap.asks),
                source=source,
                received_ts=utc_now(),
            )
        applied += 1
    return applied
