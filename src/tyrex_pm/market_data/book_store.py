"""Authoritative reconstructed Polymarket books (Option B)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from tyrex_pm.core.book_events import (
    BookDeltaReceived,
    BookSide,
    BookSnapshotReceived,
    TickSizeChanged,
)
from tyrex_pm.core.events import BookUpdated, EventSource
from tyrex_pm.core.ids import CorrelationId, EventId, InstrumentId, new_event_id
from tyrex_pm.core.snapshots import BookLevel, BookSnapshot
from tyrex_pm.engine.dispatcher import EventDispatcher


@dataclass
class BookState:
    book: BookSnapshot | None = None
    initialized: bool = False
    last_ts_event: datetime | None = None
    last_ts_received: datetime | None = None
    tick_size: Decimal | None = None
    recovery_required: bool = False


class MarketStateStore:
    """Owns reconstructed books; applies snapshot/delta/tick-size events."""

    def __init__(self, dispatcher: EventDispatcher | None = None) -> None:
        self._books: dict[InstrumentId, BookState] = {}
        self._dispatcher = dispatcher

    def attach(self, dispatcher: EventDispatcher) -> None:
        self._dispatcher = dispatcher
        dispatcher.subscribe(BookSnapshotReceived, self.on_snapshot)
        dispatcher.subscribe(BookDeltaReceived, self.on_delta)
        dispatcher.subscribe(TickSizeChanged, self.on_tick_size)

    def get(self, instrument_id: InstrumentId) -> BookState:
        return self._books.get(instrument_id, BookState())

    def on_snapshot(self, event: BookSnapshotReceived) -> None:
        state = BookState(
            book=event.book,
            initialized=True,
            last_ts_event=event.ts_event,
            last_ts_received=event.ts_received,
            tick_size=self._books.get(event.book.instrument_id, BookState()).tick_size,
            recovery_required=False,
        )
        self._books[event.book.instrument_id] = state
        self._emit_book_updated(event, state.book)

    def on_delta(self, event: BookDeltaReceived) -> None:
        # Group by instrument
        by_inst: dict[InstrumentId, list] = {}
        for change in event.changes:
            by_inst.setdefault(change.instrument_id, []).append(change)

        for instrument_id, changes in by_inst.items():
            state = self._books.get(instrument_id)
            if state is None or not state.initialized or state.book is None:
                # Cannot apply delta without a prior snapshot.
                self._books[instrument_id] = BookState(
                    book=None,
                    initialized=False,
                    last_ts_event=event.ts_event,
                    last_ts_received=event.ts_received,
                    recovery_required=True,
                )
                continue
            book = self._apply_deltas(state.book, changes, ts_event=event.ts_event)
            new_state = BookState(
                book=book,
                initialized=True,
                last_ts_event=event.ts_event,
                last_ts_received=event.ts_received,
                tick_size=state.tick_size,
                recovery_required=False,
            )
            self._books[instrument_id] = new_state
            self._emit_book_updated(event, book)

    def on_tick_size(self, event: TickSizeChanged) -> None:
        state = self._books.get(event.instrument_id, BookState())
        # Tick-size change invalidates local levels until a fresh snapshot.
        self._books[event.instrument_id] = BookState(
            book=None,
            initialized=False,
            last_ts_event=event.ts_event,
            last_ts_received=event.ts_received,
            tick_size=event.new_tick_size,
            recovery_required=True,
        )

    def invalidate(self, instrument_id: InstrumentId, *, ts_event: datetime, ts_received: datetime) -> None:
        prev = self._books.get(instrument_id, BookState())
        self._books[instrument_id] = BookState(
            book=None,
            initialized=False,
            last_ts_event=ts_event,
            last_ts_received=ts_received,
            tick_size=prev.tick_size,
            recovery_required=True,
        )

    def _emit_book_updated(self, cause: BookSnapshotReceived | BookDeltaReceived, book: BookSnapshot) -> None:
        if self._dispatcher is None:
            return
        self._dispatcher.publish(
            BookUpdated(
                event_id=new_event_id(),
                correlation_id=cause.correlation_id,
                causation_id=cause.event_id,
                ts_event=cause.ts_event,
                ts_received=cause.ts_received,
                source=EventSource.SYSTEM,
                book=book,
            )
        )

    @staticmethod
    def _apply_deltas(
        book: BookSnapshot,
        changes: list,
        *,
        ts_event: datetime,
    ) -> BookSnapshot:
        bids = {level.price: level.quantity for level in book.bids}
        asks = {level.price: level.quantity for level in book.asks}
        for change in changes:
            levels = bids if change.side is BookSide.BID else asks
            if change.size == 0:
                levels.pop(change.price, None)
            else:
                levels[change.price] = change.size
        bid_levels = tuple(
            BookLevel(price=p, quantity=q)
            for p, q in sorted(bids.items(), key=lambda item: item[0], reverse=True)
        )
        ask_levels = tuple(
            BookLevel(price=p, quantity=q)
            for p, q in sorted(asks.items(), key=lambda item: item[0])
        )
        return BookSnapshot(
            instrument_id=book.instrument_id,
            ts_event=ts_event,
            bids=bid_levels,
            asks=ask_levels,
        )
