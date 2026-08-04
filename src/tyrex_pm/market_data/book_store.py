"""Authoritative reconstructed Polymarket books (Option B).

Single owner of token-keyed book state. Strategy reads via BookView only.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.book_events import (
    BookDeltaReceived,
    BookSide,
    BookSnapshotReceived,
    TickSizeChanged,
)
from tyrex_pm.core.events import BookUpdated, EventSource
from tyrex_pm.core.ids import InstrumentId, new_event_id
from tyrex_pm.core.snapshots import BookLevel, BookSnapshot
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.market_data.binding_record import MarketBindingRecord
from tyrex_pm.market_data.book_health import (
    ConnectionHealth,
    SyncHealth,
)
from tyrex_pm.market_data.book_metrics import BookMetricsRegistry
from tyrex_pm.market_data.book_view import (
    BookView,
    LegBookSnapshot,
    side_liquidity_from_book,
)
from tyrex_pm.market_data.executable import book_quote


@dataclass
class BookState:
    book: BookSnapshot | None = None
    initialized: bool = False
    last_ts_event: datetime | None = None
    last_ts_received: datetime | None = None
    last_apply_ts: datetime | None = None
    last_apply_mono_ns: int | None = None
    tick_size: Decimal | None = None
    min_order_size: Decimal | None = None
    recovery_required: bool = False
    book_version: int = 0
    venue_hash: str | None = None
    sync_health: SyncHealth = SyncHealth.UNINITIALIZED
    connection_epoch: int = 0
    binding_id: str | None = None
    source: str | None = None  # REST_BOOTSTRAP | WS_SNAPSHOT | WS_DELTA

    def derive_legacy_flags(self) -> None:
        """Keep initialized/recovery_required aligned with sync_health."""
        if self.sync_health is SyncHealth.READY:
            self.initialized = self.book is not None
            self.recovery_required = False
        elif self.sync_health is SyncHealth.DESYNCED:
            self.initialized = False
            self.recovery_required = True
        elif self.sync_health is SyncHealth.SYNCING:
            self.initialized = False
            self.recovery_required = True
        elif self.sync_health is SyncHealth.STALE:
            self.initialized = self.book is not None
            self.recovery_required = False
        else:
            self.initialized = False
            self.recovery_required = False


class MarketStateStore:
    """Owns reconstructed books; applies snapshot/delta/tick-size events."""

    def __init__(self, dispatcher: EventDispatcher | None = None) -> None:
        self._books: dict[InstrumentId, BookState] = {}
        self._dispatcher = dispatcher
        self._lock = threading.RLock()
        self.metrics = BookMetricsRegistry()
        self._allowed_tokens: set[str] | None = None
        self._min_connection_epoch: int = 0
        self._stale_after_ms: int = 30_000

    def attach(self, dispatcher: EventDispatcher) -> None:
        self._dispatcher = dispatcher
        dispatcher.subscribe(BookSnapshotReceived, self.on_snapshot)
        dispatcher.subscribe(BookDeltaReceived, self.on_delta)
        dispatcher.subscribe(TickSizeChanged, self.on_tick_size)

    def set_allowed_tokens(self, token_ids: set[str] | None) -> None:
        with self._lock:
            self._allowed_tokens = None if token_ids is None else set(token_ids)

    def set_min_connection_epoch(self, epoch: int) -> None:
        with self._lock:
            self._min_connection_epoch = max(0, int(epoch))

    def get(self, instrument_id: InstrumentId) -> BookState:
        with self._lock:
            state = self._books.get(instrument_id)
            if state is None:
                return BookState()
            # Return a shallow copy snapshot of fields for safe read.
            return BookState(
                book=state.book,
                initialized=state.initialized,
                last_ts_event=state.last_ts_event,
                last_ts_received=state.last_ts_received,
                last_apply_ts=state.last_apply_ts,
                last_apply_mono_ns=state.last_apply_mono_ns,
                tick_size=state.tick_size,
                min_order_size=state.min_order_size,
                recovery_required=state.recovery_required,
                book_version=state.book_version,
                venue_hash=state.venue_hash,
                sync_health=state.sync_health,
                connection_epoch=state.connection_epoch,
                binding_id=state.binding_id,
                source=state.source,
            )

    def on_snapshot(self, event: BookSnapshotReceived) -> None:
        token = event.book.instrument_id.value
        counters = self.metrics.for_token(token)
        counters.snapshots_received += 1
        conn_epoch = int(getattr(event, "connection_epoch", 0) or 0)
        with self._lock:
            if self._allowed_tokens is not None and token not in self._allowed_tokens:
                counters.snapshots_identity_rejected += 1
                counters.unknown_token += 1
                return
            if conn_epoch and conn_epoch < self._min_connection_epoch:
                counters.snapshots_identity_rejected += 1
                counters.old_generation += 1
                return
            counters.snapshots_decoded += 1
            counters.snapshots_matched += 1
            prev = self._books.get(event.book.instrument_id, BookState())
            mono = time.monotonic_ns()
            now = event.ts_received
            state = BookState(
                book=event.book,
                last_ts_event=event.ts_event,
                last_ts_received=event.ts_received,
                last_apply_ts=now,
                last_apply_mono_ns=mono,
                tick_size=prev.tick_size,
                min_order_size=prev.min_order_size,
                book_version=prev.book_version + 1,
                venue_hash=event.venue_hash,
                sync_health=SyncHealth.READY,
                connection_epoch=conn_epoch or prev.connection_epoch,
                binding_id=prev.binding_id,
                source="WS_SNAPSHOT",
            )
            state.derive_legacy_flags()
            self._books[event.book.instrument_id] = state
            counters.snapshots_applied += 1
            book = state.book
        assert book is not None
        self._emit_book_updated(event, book)

    def apply_rest_snapshot(
        self,
        book: BookSnapshot,
        *,
        ts_received: datetime,
        venue_hash: str | None = None,
        tick_size: Decimal | None = None,
        min_order_size: Decimal | None = None,
        binding_id: str | None = None,
        connection_epoch: int = 0,
        request_start_version: int | None = None,
        mark_ready: bool = False,
        source: str = "REST_BOOTSTRAP",
    ) -> bool:
        """Apply REST snapshot with stale-overwrite protection.

        When ``mark_ready`` is False (default for REST-only seed before WS
        reconcile), sync_health becomes SYNCING — snapshot-available but not
        continuously synchronized READY.
        """
        token = book.instrument_id.value
        counters = self.metrics.for_token(token)
        counters.rest_requests += 1
        with self._lock:
            if self._allowed_tokens is not None and token not in self._allowed_tokens:
                counters.rest_failures += 1
                counters.unknown_token += 1
                return False
            prev = self._books.get(book.instrument_id, BookState())
            if (
                request_start_version is not None
                and prev.book_version > request_start_version
                and prev.sync_health is SyncHealth.READY
                and prev.source in {"WS_SNAPSHOT", "WS_DELTA"}
            ):
                # Newer healthy WS state won the race against late REST.
                counters.rest_failures += 1
                return False
            mono = time.monotonic_ns()
            health = SyncHealth.READY if mark_ready else SyncHealth.SYNCING
            state = BookState(
                book=book,
                last_ts_event=book.ts_event,
                last_ts_received=ts_received,
                last_apply_ts=ts_received,
                last_apply_mono_ns=mono,
                tick_size=tick_size if tick_size is not None else prev.tick_size,
                min_order_size=(
                    min_order_size if min_order_size is not None else prev.min_order_size
                ),
                book_version=prev.book_version + 1,
                venue_hash=venue_hash,
                sync_health=health,
                connection_epoch=connection_epoch or prev.connection_epoch,
                binding_id=binding_id or prev.binding_id,
                source=source,
            )
            state.derive_legacy_flags()
            # REST seed counts as initialized for legacy readers when book present.
            if state.book is not None:
                state.initialized = True
                state.recovery_required = health is SyncHealth.DESYNCED
            self._books[book.instrument_id] = state
            counters.rest_successes += 1
            if source == "REST_BOOTSTRAP":
                counters.bootstrap_count += 1
            else:
                counters.resync_count += 1
            counters.snapshots_received += 1
            counters.snapshots_decoded += 1
            counters.snapshots_matched += 1
            counters.snapshots_applied += 1
            return True

    def on_delta(self, event: BookDeltaReceived) -> None:
        conn_epoch = int(getattr(event, "connection_epoch", 0) or 0)
        by_inst: dict[InstrumentId, list] = {}
        for change in event.changes:
            by_inst.setdefault(change.instrument_id, []).append(change)

        for instrument_id, changes in by_inst.items():
            token = instrument_id.value
            counters = self.metrics.for_token(token)
            counters.deltas_received += 1
            with self._lock:
                if self._allowed_tokens is not None and token not in self._allowed_tokens:
                    counters.deltas_identity_rejected += 1
                    counters.unknown_token += 1
                    continue
                if conn_epoch and conn_epoch < self._min_connection_epoch:
                    counters.deltas_identity_rejected += 1
                    counters.old_generation += 1
                    continue
                counters.deltas_decoded += 1
                counters.deltas_matched += 1
                state = self._books.get(instrument_id)
                if state is None or not state.initialized or state.book is None:
                    self._books[instrument_id] = BookState(
                        book=None,
                        last_ts_event=event.ts_event,
                        last_ts_received=event.ts_received,
                        sync_health=SyncHealth.DESYNCED,
                        recovery_required=True,
                        connection_epoch=conn_epoch,
                    )
                    self._books[instrument_id].derive_legacy_flags()
                    counters.deltas_application_rejected += 1
                    continue
                if state.sync_health not in {
                    SyncHealth.READY,
                    SyncHealth.STALE,
                    SyncHealth.SYNCING,
                }:
                    counters.deltas_application_rejected += 1
                    continue
                # Meaningful change detection for version bump / noop.
                before = {
                    ("BID", lv.price): lv.quantity for lv in state.book.bids
                }
                before.update({("ASK", lv.price): lv.quantity for lv in state.book.asks})
                book = self._apply_deltas(state.book, changes, ts_event=event.ts_event)
                after = {("BID", lv.price): lv.quantity for lv in book.bids}
                after.update({("ASK", lv.price): lv.quantity for lv in book.asks})
                if before == after:
                    counters.deltas_ignored_noop += 1
                    continue
                for key, size in after.items():
                    if key not in before:
                        counters.levels_inserted += 1
                    elif before[key] != size:
                        counters.levels_updated += 1
                for key in before:
                    if key not in after:
                        counters.levels_deleted += 1
                mono = time.monotonic_ns()
                new_state = BookState(
                    book=book,
                    last_ts_event=event.ts_event,
                    last_ts_received=event.ts_received,
                    last_apply_ts=event.ts_received,
                    last_apply_mono_ns=mono,
                    tick_size=state.tick_size,
                    min_order_size=state.min_order_size,
                    book_version=state.book_version + 1,
                    venue_hash=state.venue_hash,
                    sync_health=SyncHealth.READY,
                    connection_epoch=conn_epoch or state.connection_epoch,
                    binding_id=state.binding_id,
                    source="WS_DELTA",
                )
                new_state.derive_legacy_flags()
                self._books[instrument_id] = new_state
                counters.deltas_applied += 1
                out_book = book
            self._emit_book_updated(event, out_book)

    def on_tick_size(self, event: TickSizeChanged) -> None:
        with self._lock:
            prev = self._books.get(event.instrument_id, BookState())
            state = BookState(
                book=None,
                last_ts_event=event.ts_event,
                last_ts_received=event.ts_received,
                tick_size=event.new_tick_size,
                min_order_size=prev.min_order_size,
                book_version=prev.book_version + 1,
                sync_health=SyncHealth.DESYNCED,
                connection_epoch=prev.connection_epoch,
                binding_id=prev.binding_id,
                source=prev.source,
            )
            state.derive_legacy_flags()
            self._books[event.instrument_id] = state

    def invalidate(
        self,
        instrument_id: InstrumentId,
        *,
        ts_event: datetime,
        ts_received: datetime,
        sync_health: SyncHealth = SyncHealth.DESYNCED,
    ) -> None:
        with self._lock:
            prev = self._books.get(instrument_id, BookState())
            state = BookState(
                book=None,
                last_ts_event=ts_event,
                last_ts_received=ts_received,
                tick_size=prev.tick_size,
                min_order_size=prev.min_order_size,
                book_version=prev.book_version + 1,
                sync_health=sync_health,
                connection_epoch=prev.connection_epoch,
                binding_id=prev.binding_id,
            )
            state.derive_legacy_flags()
            self._books[instrument_id] = state

    def mark_stale_if_aged(self, *, now_mono_ns: int | None = None) -> None:
        mono = time.monotonic_ns() if now_mono_ns is None else now_mono_ns
        threshold_ns = self._stale_after_ms * 1_000_000
        with self._lock:
            for iid, state in list(self._books.items()):
                if state.sync_health is not SyncHealth.READY:
                    continue
                if state.last_apply_mono_ns is None:
                    continue
                if mono - state.last_apply_mono_ns > threshold_ns:
                    state.sync_health = SyncHealth.STALE
                    state.derive_legacy_flags()
                    self._books[iid] = state

    def capture_pair(
        self,
        binding: MarketBindingRecord,
        *,
        connection_epoch: int = 0,
        connection_health: ConnectionHealth = ConnectionHealth.CLOSED,
        captured_at: datetime | None = None,
    ) -> BookView:
        """Atomic immutable two-leg view under the store lock."""
        at = captured_at or datetime.now(timezone.utc)
        with self._lock:
            up = self._leg_snapshot(
                binding.up_instrument_id,
                token_id=binding.up_token_id,
                connection_health=connection_health,
                now=at,
            )
            down = self._leg_snapshot(
                binding.down_instrument_id,
                token_id=binding.down_token_id,
                connection_health=connection_health,
                now=at,
            )
            return BookView(
                binding_id=binding.binding_id,
                role_epoch=binding.role_epoch,
                window_slug=binding.window_slug,
                condition_id=binding.condition_id,
                connection_epoch=connection_epoch,
                captured_at=at,
                pair_version=(up.book_version, down.book_version),
                up=up,
                down=down,
            )

    def _leg_snapshot(
        self,
        instrument_id: InstrumentId,
        *,
        token_id: str,
        connection_health: ConnectionHealth,
        now: datetime,
    ) -> LegBookSnapshot:
        state = self._books.get(instrument_id, BookState())
        age_ms = None
        if state.last_apply_ts is not None:
            age_ms = int((now - state.last_apply_ts).total_seconds() * 1000)
        return LegBookSnapshot(
            token_id=token_id,
            instrument_id=instrument_id,
            book=state.book,
            book_version=state.book_version,
            venue_hash=state.venue_hash,
            sync_health=state.sync_health,
            connection_health=connection_health,
            bid_liquidity=side_liquidity_from_book(state.book, side="BID"),
            ask_liquidity=side_liquidity_from_book(state.book, side="ASK"),
            tick_size=state.tick_size,
            min_order_size=state.min_order_size,
            source_ts=state.last_ts_event,
            receive_ts=state.last_ts_received,
            apply_ts=state.last_apply_ts,
            apply_mono_ns=state.last_apply_mono_ns,
            data_age_ms=age_ms,
            quote=book_quote(state.book),
        )

    def _emit_book_updated(
        self, cause: BookSnapshotReceived | BookDeltaReceived, book: BookSnapshot
    ) -> None:
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
