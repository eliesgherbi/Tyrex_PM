"""MarketStateStore v2 capture APIs (Phase 2 M2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.market_data.models import BookSource
from tyrex_pm.state.market_store import BookLevel, MarketStateStore

TOKEN = TokenId("tok-v2")


def test_capture_immutable_metadata() -> None:
    store = MarketStateStore()
    ts = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)
    sid = store.apply_book(
        TOKEN,
        [BookLevel(Decimal("0.48"), Decimal("10"))],
        [BookLevel(Decimal("0.52"), Decimal("20"))],
        source=BookSource.REST_BOOTSTRAP,
        received_ts=ts,
        exchange_ts=ts,
        book_hash="0xabc",
    )
    cap = store.capture(TOKEN, now=ts + timedelta(seconds=1))
    assert cap is not None
    assert cap.snapshot_id == sid
    assert cap.source == BookSource.REST_BOOTSTRAP
    assert cap.book_age_ms == 1000
    assert cap.best_bid == Decimal("0.48")
    assert cap.best_ask == Decimal("0.52")
    assert cap.book_hash == "0xabc"


def test_snapshot_id_unique_per_apply() -> None:
    store = MarketStateStore()
    s1 = store.apply_book(TOKEN, [], [BookLevel(Decimal("0.5"), Decimal("1"))], source=BookSource.WEBSOCKET)
    s2 = store.apply_book(TOKEN, [], [BookLevel(Decimal("0.51"), Decimal("1"))], source=BookSource.WEBSOCKET)
    assert s1 != s2


def test_book_age_ms_from_received_ts() -> None:
    store = MarketStateStore()
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.apply_book(TOKEN, [BookLevel(Decimal("0.4"), Decimal("1"))], [], received_ts=ts, source=BookSource.REST_POLL)
    assert store.book_age_ms(TOKEN, now=ts + timedelta(milliseconds=250)) == 250


def test_capture_pair_requires_both_legs() -> None:
    store = MarketStateStore()
    yes = TokenId("yes")
    no = TokenId("no")
    store.apply_book(yes, [BookLevel(Decimal("0.5"), Decimal("1"))], [], source=BookSource.WEBSOCKET)
    assert store.capture_pair(yes, no, "pair1") is None
    store.apply_book(no, [BookLevel(Decimal("0.4"), Decimal("1"))], [], source=BookSource.WEBSOCKET)
    pair = store.capture_pair(yes, no, "pair1")
    assert pair is not None
    assert pair.pair_id == "pair1"
    assert pair.yes.token_id == yes
    assert pair.no.token_id == no


def test_reconnect_gap_state() -> None:
    store = MarketStateStore()
    store.set_reconnect_gap(TOKEN, True)
    assert store.reconnect_gap(TOKEN) is True
    store.apply_book(TOKEN, [BookLevel(Decimal("0.5"), Decimal("1"))], [], source=BookSource.WEBSOCKET, reconnect_gap=False)
    assert store.reconnect_gap(TOKEN) is False


def test_backward_compat_snapshot_and_is_stale() -> None:
    store = MarketStateStore(default_max_age_s=5.0)
    from tyrex_pm.state.market_store import make_snapshot

    store.apply_snapshot(make_snapshot(TOKEN, bids=[(Decimal("0.49"), Decimal("1"))], asks=[(Decimal("0.51"), Decimal("1"))]))
    assert store.snapshot(TOKEN) is not None
    assert store.is_stale(TOKEN) is False
    cap = store.capture(TOKEN)
    assert cap is not None
    assert cap.source == BookSource.REST_POLL
