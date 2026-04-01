"""Resolve allowlisted market slugs via Nautilus PolymarketDataLoader + public CLOB book."""

from __future__ import annotations

from datetime import UTC, datetime

from nautilus_trader.adapters.polymarket import PolymarketDataLoader
from py_clob_client.client import ClobClient

from tyrex_pm.core.market_types import ResolvedMarket
from tyrex_pm.data.book_check import summarize_book_sides, tick_size_matches_instrument

DEFAULT_CLOB_HOST = "https://clob.polymarket.com"
DEFAULT_CHAIN_ID = 137


async def resolve_market_slug(
    slug: str,
    *,
    clob_host: str = DEFAULT_CLOB_HOST,
    chain_id: int = DEFAULT_CHAIN_ID,
) -> ResolvedMarket:
    loader = await PolymarketDataLoader.from_market_slug(slug)
    inst = loader.instrument
    info = getattr(inst, "info", None) or {}
    neg = info.get("neg_risk")
    if neg is not None:
        neg = bool(neg)
    min_tick = info.get("minimum_tick_size")
    min_tick_s = str(min_tick) if min_tick is not None else None

    price_inc = str(inst.price_increment)
    size_inc = str(inst.size_increment)
    token_id = str(loader.token_id)
    now = datetime.now(tz=UTC).isoformat()

    ro = ClobClient(clob_host, chain_id=chain_id)
    try:
        book = ro.get_order_book(token_id)
    except Exception as exc:  # noqa: BLE001 — surface venue errors for operators
        return ResolvedMarket(
            slug=slug,
            instrument_id=str(inst.id),
            token_id=token_id,
            price_increment=price_inc,
            size_increment=size_inc,
            neg_risk=neg,
            minimum_tick_size=min_tick_s,
            book_status="book_error",
            book_detail=type(exc).__name__,
            clob_tick_size=None,
            resolved_at_utc=now,
        )

    st, detail = summarize_book_sides(book.bids, book.asks)
    clob_tick = book.tick_size
    if not tick_size_matches_instrument(price_inc, clob_tick):
        detail = f"{detail}_tick_mismatch_instr={price_inc}_clob={clob_tick}"

    return ResolvedMarket(
        slug=slug,
        instrument_id=str(inst.id),
        token_id=token_id,
        price_increment=price_inc,
        size_increment=size_inc,
        neg_risk=neg,
        minimum_tick_size=min_tick_s,
        book_status=st,
        book_detail=detail,
        clob_tick_size=str(clob_tick) if clob_tick is not None else None,
        resolved_at_utc=now,
    )
