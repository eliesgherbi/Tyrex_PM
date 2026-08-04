"""Deterministic official polymarket-client adapter contract tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from tyrex_pm.adapters.polymarket.rest_book import (
    bootstrap_token_into_store,
    normalize_rest_book_payload,
)
from tyrex_pm.adapters.polymarket.sdk_errors import (
    PolymarketErrorCategory,
    classify_polymarket_error,
)
from tyrex_pm.adapters.polymarket.sdk_public import (
    order_book_to_rest_payload,
    sdk_market_event_to_tyrex,
)
from tyrex_pm.core.book_events import BookDeltaReceived, BookSnapshotReceived
from tyrex_pm.core.ids import InstrumentId
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.market_data.binding_record import (
    BindingLifecycleRole,
    MarketBindingRecord,
    make_binding_id,
)
from tyrex_pm.market_data.book_feed import BindingFeed
from tyrex_pm.market_data.book_health import FeedSyncPhase, SyncHealth
from tyrex_pm.market_data.book_store import MarketStateStore

TS = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
UP = "111"
DOWN = "222"


def _binding() -> MarketBindingRecord:
    return MarketBindingRecord(
        binding_id=make_binding_id(window_slug="btc-updown-5m-9", condition_id="0xcond"),
        window_slug="btc-updown-5m-9",
        condition_id="0xcond",
        market_id="0xcond",
        up_token_id=UP,
        down_token_id=DOWN,
        event_start=TS,
        event_end=TS,
        role=BindingLifecycleRole.ACTIVE,
        role_epoch=0,
        outcome_semantics="UP_DOWN",
        resolved_at=TS,
    )


@dataclass
class _Level:
    price: Decimal
    size: Decimal


@dataclass
class _OrderBook:
    token_id: str
    bids: tuple[_Level, ...]
    asks: tuple[_Level, ...]
    tick_size: Decimal = Decimal("0.01")
    min_order_size: Decimal = Decimal("5")
    hash: str = "h1"
    timestamp: datetime | None = TS
    condition_id: str = "0xcond"


def test_sdk_order_book_conversion_decimal_and_ordering() -> None:
    ob = _OrderBook(
        token_id=UP,
        bids=(_Level(Decimal("0.40"), Decimal("10")), _Level(Decimal("0.45"), Decimal("3"))),
        asks=(_Level(Decimal("0.60"), Decimal("8")), _Level(Decimal("0.55"), Decimal("2"))),
    )
    payload = order_book_to_rest_payload(ob, expected_token_id=UP)
    assert payload.book.best_bid is not None and payload.book.best_bid.price == Decimal("0.45")
    assert payload.book.best_ask is not None and payload.book.best_ask.price == Decimal("0.55")
    assert isinstance(payload.book.best_bid.price, Decimal)
    assert payload.tick_size == Decimal("0.01")


def test_sdk_order_book_token_mismatch() -> None:
    ob = _OrderBook(token_id=UP, bids=(), asks=())
    with pytest.raises(ValueError, match="sdk_token_mismatch"):
        order_book_to_rest_payload(ob, expected_token_id=DOWN)


def test_sdk_order_book_empty_sides() -> None:
    ob = _OrderBook(token_id=UP, bids=(), asks=())
    payload = order_book_to_rest_payload(ob, expected_token_id=UP)
    assert payload.book.bids == ()
    assert payload.book.asks == ()
    assert payload.book.best_ask is None


def test_sdk_book_event_and_multi_price_change_and_size_zero() -> None:
    book_ev = SimpleNamespace(
        type="book",
        payload=SimpleNamespace(
            token_id=UP,
            timestamp=TS,
            hash="h",
            bids=(_Level(Decimal("0.5"), Decimal("9")),),
            asks=(_Level(Decimal("0.6"), Decimal("4")),),
        ),
    )
    snap = sdk_market_event_to_tyrex(book_ev, ts_received=TS)
    assert isinstance(snap, BookSnapshotReceived)
    assert snap.book.best_bid.price == Decimal("0.5")

    pc = SimpleNamespace(
        type="price_change",
        payload=SimpleNamespace(
            timestamp=TS,
            price_changes=(
                SimpleNamespace(
                    token_id=UP, side="BUY", price=Decimal("0.5"), size=Decimal("0")
                ),
                SimpleNamespace(
                    token_id=UP, side="SELL", price=Decimal("0.61"), size=Decimal("2")
                ),
            ),
        ),
    )
    delta = sdk_market_event_to_tyrex(pc, ts_received=TS)
    assert isinstance(delta, BookDeltaReceived)
    assert len(delta.changes) == 2
    assert delta.changes[0].size == Decimal("0")


def test_sdk_unknown_event_type_returns_none() -> None:
    ev = SimpleNamespace(type="last_trade_price", payload=SimpleNamespace())
    assert sdk_market_event_to_tyrex(ev, ts_received=TS) is None


def test_classify_user_input_and_rate_limit() -> None:
    from polymarket import RateLimitError, UserInputError

    c1 = classify_polymarket_error(UserInputError("bad"))
    assert c1.category is PolymarketErrorCategory.USER_INPUT
    assert c1.retryable is False
    c2 = classify_polymarket_error(RateLimitError("slow"))
    assert c2.category is PolymarketErrorCategory.RATE_LIMIT
    assert c2.retryable is True


def test_stale_rest_rejection_still_holds() -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)
    from tyrex_pm.core.book_events import BookSnapshotReceived
    from tyrex_pm.core.events import EventSource
    from tyrex_pm.core.ids import new_correlation_id, new_event_id
    from tyrex_pm.core.snapshots import BookLevel, BookSnapshot

    book = BookSnapshot(
        instrument_id=InstrumentId(UP),
        ts_event=TS,
        bids=(BookLevel(price=Decimal("0.41"), quantity=Decimal("1")),),
        asks=(BookLevel(price=Decimal("0.59"), quantity=Decimal("1")),),
    )
    disp.publish(
        BookSnapshotReceived(
            event_id=new_event_id(),
            correlation_id=new_correlation_id(),
            ts_event=TS,
            ts_received=TS,
            source=EventSource.POLYMARKET_CLOB,
            book=book,
            connection_epoch=1,
        )
    )
    v = store.get(InstrumentId(UP)).book_version
    older = normalize_rest_book_payload(
        {
            "asset_id": UP,
            "timestamp": "1740000000000",
            "bids": [{"price": "0.1", "size": "1"}],
            "asks": [{"price": "0.9", "size": "1"}],
        }
    )
    ok = store.apply_rest_snapshot(
        older.book,
        ts_received=TS,
        request_start_version=0,
        mark_ready=True,
        source="REST_BOOTSTRAP",
    )
    assert ok is False
    assert store.get(InstrumentId(UP)).book_version == v


@pytest.mark.asyncio
async def test_desynced_recovers_from_ws_full_book_when_rest_fails() -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)

    def bad_fetcher(token_id: str):
        raise RuntimeError(f"rest_book_http_403:{token_id}")

    events = [
        SimpleNamespace(
            type="book",
            payload=SimpleNamespace(
                token_id=UP,
                timestamp=TS,
                hash="u",
                bids=(_Level(Decimal("0.4"), Decimal("1")),),
                asks=(_Level(Decimal("0.6"), Decimal("1")),),
            ),
        ),
        SimpleNamespace(
            type="book",
            payload=SimpleNamespace(
                token_id=DOWN,
                timestamp=TS,
                hash="d",
                bids=(_Level(Decimal("0.3"), Decimal("1")),),
                asks=(_Level(Decimal("0.7"), Decimal("1")),),
            ),
        ),
    ]

    class _Stream:
        def __init__(self) -> None:
            self._i = 0
            self._hold = __import__("asyncio").Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._i < len(events):
                ev = events[self._i]
                self._i += 1
                return ev
            await self._hold.wait()
            raise StopAsyncIteration

        async def close(self) -> None:
            self._hold.set()

    async def factory(_tokens):
        return _Stream()

    feed = BindingFeed(
        binding=_binding(),
        store=store,
        dispatcher=disp,
        rest_fetcher=bad_fetcher,
        market_stream_factory=factory,
    )
    await feed.start()
    for _ in range(50):
        if feed.phase is FeedSyncPhase.READY:
            break
        await __import__("asyncio").sleep(0.05)
    assert feed.phase is FeedSyncPhase.READY
    assert store.get(InstrumentId(UP)).book is not None
    assert store.get(InstrumentId(DOWN)).book is not None
    await feed.stop()


@pytest.mark.asyncio
async def test_desynced_buffers_without_publishing_unsafe_deltas() -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)

    feed = BindingFeed(
        binding=_binding(),
        store=store,
        dispatcher=disp,
        rest_fetcher=lambda t: (_ for _ in ()).throw(RuntimeError("rest_down")),
    )
    feed.phase = FeedSyncPhase.DESYNCED
    feed.connection_epoch = 1
    delta = sdk_market_event_to_tyrex(
        SimpleNamespace(
            type="price_change",
            payload=SimpleNamespace(
                timestamp=TS,
                price_changes=(
                    SimpleNamespace(
                        token_id=UP, side="SELL", price=Decimal("0.55"), size=Decimal("1")
                    ),
                ),
            ),
        ),
        ts_received=TS,
    )
    assert isinstance(delta, BookDeltaReceived)
    await feed._ingress(delta, connection_epoch=1)
    assert feed.phase is FeedSyncPhase.DESYNCED
    assert store.get(InstrumentId(UP)).book is None
    assert len(feed.buffer) >= 1


@pytest.mark.asyncio
async def test_buffer_overflow_marks_desynced() -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    feed = BindingFeed(
        binding=_binding(),
        store=store,
        dispatcher=disp,
        buffer_max_events=2,
        rest_fetcher=lambda t: normalize_rest_book_payload(
            {
                "asset_id": t,
                "timestamp": "1750000000000",
                "bids": [{"price": "0.4", "size": "1"}],
                "asks": [{"price": "0.6", "size": "1"}],
            },
            token_id=t,
        ),
    )
    feed.phase = FeedSyncPhase.WS_BUFFERING
    for i in range(3):
        ev = sdk_market_event_to_tyrex(
            SimpleNamespace(
                type="price_change",
                payload=SimpleNamespace(
                    timestamp=TS,
                    price_changes=(
                        SimpleNamespace(
                            token_id=UP,
                            side="BUY",
                            price=Decimal("0.4"),
                            size=Decimal(str(i + 1)),
                        ),
                    ),
                ),
            ),
            ts_received=TS,
        )
        feed._buffer_event(ev, connection_epoch=1)
    assert feed.phase is FeedSyncPhase.DESYNCED
    assert feed.last_error == "buffer_overflow"


def test_bootstrap_failsoft_per_token() -> None:
    store = MarketStateStore()
    seen: list[str] = []

    def fetcher(token_id: str):
        seen.append(token_id)
        if token_id == UP:
            raise RuntimeError("rest_book_http_403")
        return normalize_rest_book_payload(
            {
                "asset_id": token_id,
                "timestamp": "1750000000000",
                "bids": [{"price": "0.3", "size": "1"}],
                "asks": [{"price": "0.7", "size": "1"}],
            },
            token_id=token_id,
        )

    import asyncio

    feed = BindingFeed(
        binding=_binding(), store=store, dispatcher=EventDispatcher(), rest_fetcher=fetcher
    )

    async def _run():
        ok = await feed.bootstrap_rest(mark_ready=False)
        assert ok is False
        assert seen == [UP, DOWN]
        assert store.get(InstrumentId(DOWN)).book is not None
        assert store.get(InstrumentId(UP)).book is None

    asyncio.run(_run())


def test_old_connection_epoch_skipped_on_reconcile() -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)
    feed = BindingFeed(
        binding=_binding(),
        store=store,
        dispatcher=disp,
        rest_fetcher=lambda t: normalize_rest_book_payload(
            {
                "asset_id": t,
                "timestamp": "1750000000000",
                "bids": [{"price": "0.4", "size": "1"}],
                "asks": [{"price": "0.6", "size": "1"}],
            },
            token_id=t,
        ),
    )
    import asyncio

    async def _run():
        await feed.bootstrap_rest(mark_ready=True)
        feed.phase = FeedSyncPhase.RECONCILING
        snap = sdk_market_event_to_tyrex(
            SimpleNamespace(
                type="book",
                payload=SimpleNamespace(
                    token_id=UP,
                    timestamp=TS,
                    hash="x",
                    bids=(_Level(Decimal("0.41"), Decimal("1")),),
                    asks=(_Level(Decimal("0.59"), Decimal("1")),),
                ),
            ),
            ts_received=TS,
        )
        feed._buffer_event(snap, connection_epoch=0)  # old epoch
        feed.connection_epoch = 1
        await feed._reconcile_buffer(1)
        # Old epoch dropped; current book remains REST bootstrap price.
        assert store.get(InstrumentId(UP)).book.best_bid.price == Decimal("0.4")

    asyncio.run(_run())
