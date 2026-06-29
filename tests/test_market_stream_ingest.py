"""Phase 2 (architecture_enhance): market_stream ingest + REST bootstrap."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.ingestion.market_stream import apply_market_message, apply_price_change
from tyrex_pm.state.market_store import MarketStateStore, make_snapshot
from tyrex_pm.venue.polymarket.book_snapshot import (
    book_payload_to_snapshot,
    bootstrap_market_store_from_rest,
)

TOKEN = TokenId("tok-stream")


def test_market_stream_applies_book_snapshot_then_delta() -> None:
    store = MarketStateStore()
    book_msg = {
        "event_type": "book",
        "asset_id": str(TOKEN),
        "bids": [{"price": "0.49", "size": "100"}],
        "asks": [{"price": "0.51", "size": "100"}],
    }
    assert apply_market_message(store, book_msg) is True
    assert store.best_bid(TOKEN) == Decimal("0.49")
    assert store.best_ask(TOKEN) == Decimal("0.51")

    # price_change delta: improve bid, remove the old ask level (size 0), add new ask.
    delta = {
        "event_type": "price_change",
        "asset_id": str(TOKEN),
        "changes": [
            {"price": "0.50", "side": "BUY", "size": "20"},
            {"price": "0.51", "side": "SELL", "size": "0"},
            {"price": "0.52", "side": "SELL", "size": "30"},
        ],
    }
    assert apply_market_message(store, delta) is True
    assert store.best_bid(TOKEN) == Decimal("0.50")
    assert store.best_ask(TOKEN) == Decimal("0.52")


def test_market_stream_applies_book_delta_seeds_when_empty() -> None:
    store = MarketStateStore()
    delta = {
        "event_type": "price_change",
        "asset_id": str(TOKEN),
        "changes": [
            {"price": "0.40", "side": "BUY", "size": "10"},
            {"price": "0.60", "side": "SELL", "size": "10"},
        ],
    }
    assert apply_price_change(store, delta) is True
    assert store.best_bid(TOKEN) == Decimal("0.40")
    assert store.best_ask(TOKEN) == Decimal("0.60")


def test_market_stream_single_writer_only() -> None:
    # Ingestion mutates the store exclusively via apply_snapshot.
    import inspect

    from tyrex_pm.ingestion import market_stream

    src = inspect.getsource(market_stream)
    # No direct dict assignment into the store internals.
    assert "._books[" not in src
    assert src.count("apply_snapshot(") >= 1


def test_rest_snapshot_bootstraps_market_store() -> None:
    store = MarketStateStore()

    class _FakeClient:
        def get_order_book(self, token_id: str):
            return {
                "bids": [{"price": "0.45", "size": "50"}],
                "asks": [{"price": "0.55", "size": "50"}],
            }

    applied = asyncio.run(bootstrap_market_store_from_rest(store, _FakeClient(), [str(TOKEN)]))
    assert applied == 1
    assert store.best_bid(TOKEN) == Decimal("0.45")
    assert store.best_ask(TOKEN) == Decimal("0.55")


def test_book_payload_to_snapshot_filters_zero_size() -> None:
    snap = book_payload_to_snapshot(
        TOKEN,
        {"bids": [{"price": "0.4", "size": "0"}, {"price": "0.39", "size": "5"}], "asks": []},
    )
    assert snap.best_bid == Decimal("0.39")
    assert snap.best_ask is None


def test_rest_bootstrap_failsoft_on_client_error() -> None:
    store = MarketStateStore()

    class _BoomClient:
        def get_order_book(self, token_id: str):
            raise RuntimeError("boom")

    applied = asyncio.run(bootstrap_market_store_from_rest(store, _BoomClient(), [str(TOKEN)]))
    assert applied == 0
    assert store.is_stale(TOKEN) is True


def test_make_snapshot_sorts_levels() -> None:
    snap = make_snapshot(
        TOKEN,
        bids=[(Decimal("0.40"), Decimal("1")), (Decimal("0.45"), Decimal("1"))],
        asks=[(Decimal("0.60"), Decimal("1")), (Decimal("0.55"), Decimal("1"))],
    )
    assert snap.best_bid == Decimal("0.45")  # highest bid first
    assert snap.best_ask == Decimal("0.55")  # lowest ask first
