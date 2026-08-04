"""BS-1 / BS-2: binding identities, store versions, BookView."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

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
from tyrex_pm.market_data.binding_record import (
    BindingLifecycleRole,
    MarketBindingRecord,
    make_binding_id,
    persist_bindings,
    load_bindings,
)
from tyrex_pm.market_data.book_health import SideLiquidity, SyncHealth
from tyrex_pm.market_data.book_store import MarketStateStore

TS = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
UP = InstrumentId("tok-up")
DOWN = InstrumentId("tok-down")


def _evt(**extra):
    return dict(
        event_id=new_event_id(),
        correlation_id=new_correlation_id(),
        ts_event=TS,
        ts_received=TS,
        source=EventSource.POLYMARKET_CLOB,
        **extra,
    )


def _binding(*, role=BindingLifecycleRole.ACTIVE, role_epoch=0) -> MarketBindingRecord:
    return MarketBindingRecord(
        binding_id=make_binding_id(
            window_slug="btc-updown-5m-1", condition_id="0xabc"
        ),
        window_slug="btc-updown-5m-1",
        condition_id="0xabc",
        market_id="0xabc",
        up_token_id=UP.value,
        down_token_id=DOWN.value,
        event_start=TS,
        event_end=TS,
        role=role,
        role_epoch=role_epoch,
        outcome_semantics="UP_DOWN",
        resolved_at=TS,
    )


def test_binding_id_immutable_across_role_promote(tmp_path: Path) -> None:
    b = _binding(role=BindingLifecycleRole.PREPARED_NEXT, role_epoch=0)
    promoted = b.with_role(BindingLifecycleRole.ACTIVE, role_epoch=1)
    assert promoted.binding_id == b.binding_id
    assert promoted.up_token_id == b.up_token_id
    assert promoted.down_token_id == b.down_token_id
    assert promoted.role_epoch == 1
    path = tmp_path / "bindings.json"
    persist_bindings(path, [promoted])
    loaded = load_bindings(path)
    assert loaded[0].binding_id == b.binding_id


def test_duplicate_tokens_rejected() -> None:
    try:
        MarketBindingRecord(
            binding_id="x",
            window_slug="s",
            condition_id="c",
            market_id="c",
            up_token_id="same",
            down_token_id="same",
            event_start=None,
            event_end=None,
            role=BindingLifecycleRole.ACTIVE,
            role_epoch=0,
            outcome_semantics="UP_DOWN",
            resolved_at=TS,
        )
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_book_version_monotonic_and_size_zero_deletes() -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)
    book = BookSnapshot(
        instrument_id=UP,
        ts_event=TS,
        bids=(BookLevel(price=Decimal("0.40"), quantity=Decimal("10")),),
        asks=(BookLevel(price=Decimal("0.60"), quantity=Decimal("10")),),
    )
    disp.publish(BookSnapshotReceived(**_evt(), book=book))
    v1 = store.get(UP).book_version
    assert v1 == 1
    assert store.get(UP).sync_health is SyncHealth.READY
    disp.publish(
        BookDeltaReceived(
            **_evt(),
            changes=(
                BookLevelDelta(
                    instrument_id=UP,
                    side=BookSide.ASK,
                    price=Decimal("0.60"),
                    size=Decimal("0"),
                ),
            ),
        )
    )
    st = store.get(UP)
    assert st.book_version == v1 + 1
    assert st.book is not None
    assert st.book.asks == ()
    assert st.book.best_ask is None


def test_old_connection_epoch_rejected() -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)
    store.set_min_connection_epoch(2)
    book = BookSnapshot(
        instrument_id=UP,
        ts_event=TS,
        bids=(BookLevel(price=Decimal("0.4"), quantity=Decimal("1")),),
        asks=(BookLevel(price=Decimal("0.6"), quantity=Decimal("1")),),
    )
    disp.publish(BookSnapshotReceived(**_evt(connection_epoch=1), book=book))
    assert store.get(UP).sync_health is SyncHealth.UNINITIALIZED
    assert store.metrics.for_token(UP.value).old_generation == 1


def test_wrong_token_rejected() -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)
    store.set_allowed_tokens({UP.value})
    book = BookSnapshot(
        instrument_id=DOWN,
        ts_event=TS,
        bids=(),
        asks=(BookLevel(price=Decimal("0.55"), quantity=Decimal("1")),),
    )
    disp.publish(BookSnapshotReceived(**_evt(), book=book))
    assert store.get(DOWN).sync_health is SyncHealth.UNINITIALIZED
    assert store.metrics.for_token(DOWN.value).unknown_token == 1


def test_capture_pair_atomic_under_concurrent_writes() -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)
    binding = _binding()
    for iid in (UP, DOWN):
        disp.publish(
            BookSnapshotReceived(
                **_evt(),
                book=BookSnapshot(
                    instrument_id=iid,
                    ts_event=TS,
                    bids=(BookLevel(price=Decimal("0.4"), quantity=Decimal("5")),),
                    asks=(BookLevel(price=Decimal("0.6"), quantity=Decimal("5")),),
                ),
            )
        )

    stop = threading.Event()

    def writer() -> None:
        n = 0
        while not stop.is_set() and n < 200:
            disp.publish(
                BookDeltaReceived(
                    **_evt(),
                    changes=(
                        BookLevelDelta(
                            instrument_id=UP,
                            side=BookSide.ASK,
                            price=Decimal("0.61"),
                            size=Decimal(str(1 + (n % 3))),
                        ),
                    ),
                )
            )
            n += 1

    t = threading.Thread(target=writer)
    t.start()
    views = [store.capture_pair(binding) for _ in range(50)]
    stop.set()
    t.join(timeout=2)
    for view in views:
        assert view.up.book_version >= 1
        assert view.down.book_version >= 1
        assert view.pair_version == (view.up.book_version, view.down.book_version)


def test_explicit_empty_ask_liquidity_when_ready() -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)
    disp.publish(
        BookSnapshotReceived(
            **_evt(),
            book=BookSnapshot(
                instrument_id=UP,
                ts_event=TS,
                bids=(BookLevel(price=Decimal("0.4"), quantity=Decimal("1")),),
                asks=(),
            ),
        )
    )
    view = store.capture_pair(_binding())
    assert view.up.sync_health is SyncHealth.READY
    assert view.up.ask_liquidity is SideLiquidity.EXPLICITLY_EMPTY
    assert view.up.bid_liquidity is SideLiquidity.AVAILABLE
