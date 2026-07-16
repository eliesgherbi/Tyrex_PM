"""WS-primary reconnect gap handling (M8 D4)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from tyrex_pm.core.ids import TokenId
from tyrex_pm.ingestion.market_ws_ingest import _handle_sequence
from tyrex_pm.market_data.readiness import MarketReadinessState, MarketReadinessTracker
from tyrex_pm.state.market_store import BookLevel, MarketStateStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ws"
YES = TokenId("93600127953453226164027182960250028230847035010973306659500433874577010899527")


def test_out_of_order_sets_reconnect_gap_in_primary_mode() -> None:
    store = MarketStateStore()
    store.apply_book(
        YES,
        [BookLevel(Decimal("0.50"), Decimal("1"))],
        [BookLevel(Decimal("0.55"), Decimal("1"))],
        book_hash="0xbbb",
    )
    gaps: list[TokenId] = []

    def on_gap(tid: TokenId) -> None:
        gaps.append(tid)

    ok = _handle_sequence(
        store,
        YES,
        book_hash="0xaaa",
        sink=None,
        run_id=None,
        primary_mode=True,
        on_sequence_gap=on_gap,
    )
    assert ok is False
    assert store.reconnect_gap(YES)
    assert gaps


def test_reconnect_gap_blocks_readiness_trading() -> None:
    tracker = MarketReadinessTracker(yes_token_id=YES, no_token_id=TokenId("no-gap"))
    tracker.note_ws_connected()
    tracker.note_reconnect_gap()
    assert tracker.state == MarketReadinessState.PAUSED
    assert tracker.reconnect_gap
    assert not tracker.allows_trading()


def test_fresh_ws_book_clears_token_reconnect_gap() -> None:
    store = MarketStateStore()
    store.set_reconnect_gap(YES, True)
    msg = json.loads((FIXTURES / "market_book.json").read_text(encoding="utf-8"))
    from tyrex_pm.ingestion.market_stream import apply_market_message
    from tyrex_pm.market_data.models import BookSource

    apply_market_message(store, msg, source=BookSource.WEBSOCKET)
    store.set_reconnect_gap(YES, False)
    assert not store.reconnect_gap(YES)
