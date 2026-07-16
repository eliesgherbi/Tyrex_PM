"""WS-primary event ordering tests (M8 D4)."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.ingestion.market_ws_ingest import _handle_sequence
from tyrex_pm.state.market_store import BookLevel, MarketStateStore

YES = TokenId("yes-order")


def test_duplicate_hash_does_not_invoke_sequence_gap_callback() -> None:
    store = MarketStateStore()
    store.apply_book(
        YES,
        [BookLevel(Decimal("0.50"), Decimal("1"))],
        [BookLevel(Decimal("0.55"), Decimal("1"))],
        book_hash="0xabc",
    )
    gaps: list[TokenId] = []

    def on_gap(tid: TokenId) -> None:
        gaps.append(tid)

    assert not _handle_sequence(
        store,
        YES,
        book_hash="0xabc",
        sink=None,
        run_id=None,
        primary_mode=True,
        on_sequence_gap=on_gap,
    )
    assert not gaps
    assert not store.reconnect_gap(YES)


def test_duplicate_hash_ignored() -> None:
    store = MarketStateStore()
    store.apply_book(
        YES,
        [BookLevel(Decimal("0.50"), Decimal("1"))],
        [BookLevel(Decimal("0.55"), Decimal("1"))],
        book_hash="0xabc",
    )
    assert not _handle_sequence(store, YES, book_hash="0xabc", sink=None, run_id=None)


def test_in_order_hash_applied() -> None:
    store = MarketStateStore()
    store.apply_book(
        YES,
        [BookLevel(Decimal("0.50"), Decimal("1"))],
        [BookLevel(Decimal("0.55"), Decimal("1"))],
        book_hash="0xaaa",
    )
    assert _handle_sequence(store, YES, book_hash="0xbbb", sink=None, run_id=None)


def test_no_hash_always_applies() -> None:
    store = MarketStateStore()
    assert _handle_sequence(store, YES, book_hash=None, sink=None, run_id=None)
