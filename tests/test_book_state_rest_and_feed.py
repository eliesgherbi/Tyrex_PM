"""BS-3 / BS-4 / BS-6 foundations: REST normalize + feed sync helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tyrex_pm.adapters.polymarket.rest_book import (
    bootstrap_token_into_store,
    normalize_rest_book_payload,
)
from tyrex_pm.core.book_events import (
    BookDeltaReceived,
    BookLevelDelta,
    BookSide,
    BookSnapshotReceived,
)
from tyrex_pm.core.events import EventSource
from tyrex_pm.core.ids import InstrumentId, new_correlation_id, new_event_id
from tyrex_pm.core.snapshots import BookLevel, BookSnapshot
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.market_data.binding_record import BindingLifecycleRole, MarketBindingRecord, make_binding_id
from tyrex_pm.market_data.book_feed import BindingFeed, BookFeedSupervisor
from tyrex_pm.market_data.book_health import FeedSyncPhase, SideLiquidity, SyncHealth
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


def test_rest_normalizes_best_last_arrays() -> None:
    # REST docs: bids ascending (best last), asks descending (best last).
    payload = {
        "asset_id": UP,
        "timestamp": "1750000000000",
        "hash": "h1",
        "tick_size": "0.01",
        "min_order_size": "5",
        "bids": [
            {"price": "0.40", "size": "10"},
            {"price": "0.45", "size": "3"},
        ],
        "asks": [
            {"price": "0.60", "size": "8"},
            {"price": "0.55", "size": "2"},
        ],
    }
    norm = normalize_rest_book_payload(payload)
    assert norm.book.best_bid is not None and norm.book.best_bid.price == Decimal("0.45")
    assert norm.book.best_ask is not None and norm.book.best_ask.price == Decimal("0.55")
    assert norm.tick_size == Decimal("0.01")
    assert norm.min_order_size == Decimal("5")


def test_rest_empty_ask_side() -> None:
    payload = {
        "asset_id": UP,
        "timestamp": "1750000000000",
        "bids": [{"price": "0.4", "size": "1"}],
        "asks": [],
    }
    norm = normalize_rest_book_payload(payload)
    assert norm.book.asks == ()
    assert norm.book.best_ask is None


def test_bootstrap_marks_syncing_not_ready_by_default() -> None:
    store = MarketStateStore()

    def fetcher(token_id: str):
        return normalize_rest_book_payload(
            {
                "asset_id": token_id,
                "timestamp": "1750000000000",
                "bids": [{"price": "0.4", "size": "1"}],
                "asks": [{"price": "0.6", "size": "1"}],
            },
            token_id=token_id,
        )

    bootstrap_token_into_store(store, UP, binding_id="b1", mark_ready=False, fetcher=fetcher)
    st = store.get(InstrumentId(UP))
    assert st.initialized is True
    assert st.sync_health is SyncHealth.SYNCING  # not continuous READY
    assert st.book is not None
    assert st.book.best_ask is not None


def test_stale_rest_does_not_overwrite_newer_ws_ready() -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)
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
    assert store.get(InstrumentId(UP)).sync_health is SyncHealth.READY

    def fetcher(token_id: str):
        return normalize_rest_book_payload(
            {
                "asset_id": token_id,
                "timestamp": "1740000000000",
                "bids": [{"price": "0.10", "size": "1"}],
                "asks": [{"price": "0.90", "size": "1"}],
            },
            token_id=token_id,
        )

    # Simulate REST that started before WS advanced: request_start_version < current.
    ok = store.apply_rest_snapshot(
        fetcher(UP).book,
        ts_received=TS,
        request_start_version=0,
        mark_ready=True,
        source="REST_BOOTSTRAP",
    )
    # Because WS READY advanced past request_start, reject.
    assert ok is False
    assert store.get(InstrumentId(UP)).book_version == v
    assert store.get(InstrumentId(UP)).book is not None
    assert store.get(InstrumentId(UP)).book.best_ask.price == Decimal("0.59")


@pytest.mark.asyncio
async def test_supervisor_promote_preserves_binding_id_and_books(tmp_path) -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)
    payloads = {
        UP: {
            "asset_id": UP,
            "timestamp": "1750000000000",
            "bids": [{"price": "0.4", "size": "2"}],
            "asks": [{"price": "0.6", "size": "2"}],
        },
        DOWN: {
            "asset_id": DOWN,
            "timestamp": "1750000000000",
            "bids": [{"price": "0.3", "size": "2"}],
            "asks": [{"price": "0.7", "size": "2"}],
        },
    }

    def fetcher(token_id: str):
        return normalize_rest_book_payload(payloads[token_id], token_id=token_id)

    supervisor = BookFeedSupervisor(
        store=store, dispatcher=disp, out_dir=tmp_path, rest_fetcher=fetcher
    )
    prepared = _binding()
    prepared = prepared.with_role(BindingLifecycleRole.PREPARED_NEXT, role_epoch=0)
    # Avoid live WS: manually create feed and REST seed only.
    feed = BindingFeed(
        binding=prepared, store=store, dispatcher=disp, rest_fetcher=fetcher
    )
    await feed.bootstrap_rest(mark_ready=True)
    assert feed.phase in {FeedSyncPhase.RECONCILING, FeedSyncPhase.SNAPSHOT_ACQUIRING}
    # Force READY books for promote test.
    for tok in (UP, DOWN):
        st = store.get(InstrumentId(tok))
        assert st.book is not None
        store.apply_rest_snapshot(
            st.book,
            ts_received=TS,
            mark_ready=True,
            binding_id=prepared.binding_id,
            source="REST_RECONCILED",
        )
    supervisor.prepared = feed
    v_before = store.get(InstrumentId(UP)).book_version
    active = await supervisor.promote_prepared()
    assert active.binding.binding_id == prepared.binding_id
    assert active.binding.role is BindingLifecycleRole.ACTIVE
    assert store.get(InstrumentId(UP)).book_version == v_before
    assert store.get(InstrumentId(UP)).book is not None
    assert (tmp_path / "bindings.json").exists()


def test_size_zero_delta_official_semantics() -> None:
    """Official/fixture rule: price_change size=0 removes the level."""
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)
    disp.publish(
        BookSnapshotReceived(
            event_id=new_event_id(),
            correlation_id=new_correlation_id(),
            ts_event=TS,
            ts_received=TS,
            source=EventSource.POLYMARKET_CLOB,
            book=BookSnapshot(
                instrument_id=InstrumentId(UP),
                ts_event=TS,
                bids=(BookLevel(price=Decimal("0.5"), quantity=Decimal("9")),),
                asks=(BookLevel(price=Decimal("0.55"), quantity=Decimal("4")),),
            ),
        )
    )
    disp.publish(
        BookDeltaReceived(
            event_id=new_event_id(),
            correlation_id=new_correlation_id(),
            ts_event=TS,
            ts_received=TS,
            source=EventSource.POLYMARKET_CLOB,
            changes=(
                BookLevelDelta(
                    instrument_id=InstrumentId(UP),
                    side=BookSide.ASK,
                    price=Decimal("0.55"),
                    size=Decimal("0"),
                ),
            ),
        )
    )
    st = store.get(InstrumentId(UP))
    assert st.book is not None
    assert st.book.asks == ()
    assert store.metrics.for_token(UP).levels_deleted >= 1
    view = store.capture_pair(_binding())
    assert view.up.ask_liquidity is SideLiquidity.EXPLICITLY_EMPTY
