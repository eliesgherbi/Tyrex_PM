"""Market-state store snapshot/delta/reconnect semantics."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.book_events import (
    BookDeltaReceived,
    BookLevelDelta,
    BookSide,
    BookSnapshotReceived,
    TickSizeChanged,
)
from tyrex_pm.core.events import BookUpdated, EventSource
from tyrex_pm.core.ids import InstrumentId, new_correlation_id, new_event_id
from tyrex_pm.core.snapshots import BookLevel, BookSnapshot
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.market_data.book_store import MarketStateStore

TS = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
IID = InstrumentId("tok-yes-1")


def _evt_kwargs():
    return dict(
        event_id=new_event_id(),
        correlation_id=new_correlation_id(),
        ts_event=TS,
        ts_received=TS,
        source=EventSource.POLYMARKET_CLOB,
    )


def test_snapshot_then_delta_and_emit_book_updated() -> None:
    dispatcher = EventDispatcher()
    store = MarketStateStore()
    store.attach(dispatcher)
    seen: list[BookUpdated] = []
    dispatcher.subscribe(BookUpdated, lambda e: seen.append(e))

    book = BookSnapshot(
        instrument_id=IID,
        ts_event=TS,
        bids=(BookLevel(price=Decimal("0.40"), quantity=Decimal("10")),),
        asks=(BookLevel(price=Decimal("0.60"), quantity=Decimal("10")),),
    )
    dispatcher.publish(BookSnapshotReceived(**_evt_kwargs(), book=book))
    assert store.get(IID).initialized is True
    assert len(seen) == 1

    dispatcher.publish(
        BookDeltaReceived(
            **_evt_kwargs(),
            changes=(
                BookLevelDelta(
                    instrument_id=IID,
                    side=BookSide.BID,
                    price=Decimal("0.41"),
                    size=Decimal("5"),
                ),
                BookLevelDelta(
                    instrument_id=IID,
                    side=BookSide.BID,
                    price=Decimal("0.40"),
                    size=Decimal("0"),
                ),
            ),
        )
    )
    state = store.get(IID)
    assert state.book is not None
    assert state.book.best_bid is not None
    assert state.book.best_bid.price == Decimal("0.41")
    assert len(seen) == 2


def test_delta_without_snapshot_requires_recovery() -> None:
    store = MarketStateStore()
    dispatcher = EventDispatcher()
    store.attach(dispatcher)
    dispatcher.publish(
        BookDeltaReceived(
            **_evt_kwargs(),
            changes=(
                BookLevelDelta(
                    instrument_id=IID,
                    side=BookSide.ASK,
                    price=Decimal("0.5"),
                    size=Decimal("1"),
                ),
            ),
        )
    )
    state = store.get(IID)
    assert state.initialized is False
    assert state.recovery_required is True


def test_tick_size_invalidates() -> None:
    store = MarketStateStore()
    dispatcher = EventDispatcher()
    store.attach(dispatcher)
    book = BookSnapshot(
        instrument_id=IID,
        ts_event=TS,
        bids=(BookLevel(price=Decimal("0.40"), quantity=Decimal("1")),),
        asks=(BookLevel(price=Decimal("0.60"), quantity=Decimal("1")),),
    )
    dispatcher.publish(BookSnapshotReceived(**_evt_kwargs(), book=book))
    dispatcher.publish(
        TickSizeChanged(
            **_evt_kwargs(),
            instrument_id=IID,
            old_tick_size=Decimal("0.01"),
            new_tick_size=Decimal("0.001"),
        )
    )
    state = store.get(IID)
    assert state.initialized is False
    assert state.recovery_required is True
    assert state.tick_size == Decimal("0.001")


def test_reconnect_invalidate() -> None:
    store = MarketStateStore()
    book = BookSnapshot(
        instrument_id=IID,
        ts_event=TS,
        bids=(BookLevel(price=Decimal("0.40"), quantity=Decimal("1")),),
        asks=(BookLevel(price=Decimal("0.60"), quantity=Decimal("1")),),
    )
    dispatcher = EventDispatcher()
    store.attach(dispatcher)
    dispatcher.publish(BookSnapshotReceived(**_evt_kwargs(), book=book))
    store.invalidate(IID, ts_event=TS, ts_received=TS)
    assert store.get(IID).initialized is False
    assert store.get(IID).recovery_required is True
