"""BS-6 protocol/golden coverage for WS book normalize + store apply."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.adapters.polymarket.normalize import normalize_market_ws_message
from tyrex_pm.core.book_events import BookDeltaReceived, BookSnapshotReceived
from tyrex_pm.core.ids import InstrumentId
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.market_data.book_store import MarketStateStore

TS = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
UP = "999001"


def test_official_style_book_snapshot_and_price_changes() -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)

    book_msg = {
        "event_type": "book",
        "asset_id": UP,
        "market": "0xcond",
        "timestamp": "1750000000000",
        "hash": "abc",
        "bids": [{"price": "0.48", "size": "30"}, {"price": "0.47", "size": "10"}],
        "asks": [{"price": "0.52", "size": "25"}, {"price": "0.53", "size": "15"}],
    }
    snap = normalize_market_ws_message(book_msg, ts_received=TS)
    assert isinstance(snap, BookSnapshotReceived)
    from dataclasses import replace

    disp.publish(replace(snap, connection_epoch=1))
    st = store.get(InstrumentId(UP))
    assert st.book is not None
    assert st.book.best_bid is not None and st.book.best_bid.price == Decimal("0.48")
    assert st.book.best_ask is not None and st.book.best_ask.price == Decimal("0.52")

    # Multiple changes in one message including size=0 delete (official/fixture rule).
    pc = {
        "event_type": "price_change",
        "market": "0xcond",
        "timestamp": "1750000001000",
        "price_changes": [
            {"asset_id": UP, "price": "0.52", "size": "0", "side": "SELL"},
            {"asset_id": UP, "price": "0.53", "size": "40", "side": "SELL"},
            {"asset_id": UP, "price": "0.49", "size": "5", "side": "BUY"},
        ],
    }
    delta = normalize_market_ws_message(pc, ts_received=TS)
    assert isinstance(delta, BookDeltaReceived)
    disp.publish(replace(delta, connection_epoch=1))
    st2 = store.get(InstrumentId(UP))
    assert st2.book is not None
    assert st2.book.best_ask is not None
    assert st2.book.best_ask.price == Decimal("0.53")
    assert any(lv.price == Decimal("0.49") for lv in st2.book.bids)
    assert store.metrics.for_token(UP).reconcile_ok()


def test_unknown_token_and_old_epoch_rejected() -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)
    store.set_allowed_tokens({UP})
    store.set_min_connection_epoch(2)

    msg = {
        "event_type": "book",
        "asset_id": "unknown-token",
        "timestamp": "1750000000000",
        "bids": [{"price": "0.4", "size": "1"}],
        "asks": [{"price": "0.6", "size": "1"}],
    }
    ev = normalize_market_ws_message(msg, ts_received=TS)
    assert isinstance(ev, BookSnapshotReceived)
    from dataclasses import replace

    disp.publish(replace(ev, connection_epoch=2))
    assert store.get(InstrumentId("unknown-token")).book is None

    good = {
        "event_type": "book",
        "asset_id": UP,
        "timestamp": "1750000000000",
        "bids": [{"price": "0.4", "size": "1"}],
        "asks": [{"price": "0.6", "size": "1"}],
    }
    ev2 = normalize_market_ws_message(good, ts_received=TS)
    disp.publish(replace(ev2, connection_epoch=1))  # old epoch
    assert store.get(InstrumentId(UP)).book is None
    disp.publish(replace(ev2, connection_epoch=2))
    assert store.get(InstrumentId(UP)).book is not None


def test_malformed_and_unsupported_events_do_not_crash() -> None:
    import pytest

    with pytest.raises(ValueError):
        normalize_market_ws_message({"event_type": "book"}, ts_received=TS)
    # Unsupported / non-book events are ignored (None) rather than applied.
    assert (
        normalize_market_ws_message(
            {"event_type": "new_market", "id": "x"}, ts_received=TS
        )
        is None
    )
