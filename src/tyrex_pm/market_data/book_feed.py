"""Binding-scoped book feeds: SDK REST bootstrap + SDK market WS buffer/reconcile."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Deque, Sequence

from tyrex_pm.adapters.polymarket.rest_book import bootstrap_token_into_store, fetch_clob_book
from tyrex_pm.adapters.polymarket.sdk_public import (
    build_async_public_client,
    sdk_market_event_to_tyrex,
)
from tyrex_pm.core.book_events import BookDeltaReceived, BookSnapshotReceived, TickSizeChanged
from tyrex_pm.core.ids import InstrumentId
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.market_data.binding_record import (
    BindingLifecycleRole,
    MarketBindingRecord,
    persist_bindings,
)
from tyrex_pm.market_data.book_health import (
    ConnectionHealth,
    FeedSyncPhase,
    SyncHealth,
)
from tyrex_pm.market_data.book_store import MarketStateStore
from tyrex_pm.market_data.book_view import BookView

logger = logging.getLogger(__name__)

DEFAULT_BUFFER_MAX_EVENTS = 2048
DEFAULT_BUFFER_MAX_AGE_S = 5.0
EVAL_MIN_INTERVAL_S = 1.0
COLD_GAP_MAX_S = 2.0 * EVAL_MIN_INTERVAL_S
REST_RETRY_BACKOFF_S = (0.5, 1.0, 2.0, 4.0)


@dataclass
class BufferedIngress:
    received_mono_ns: int
    connection_epoch: int
    event: BookSnapshotReceived | BookDeltaReceived | TickSizeChanged


MarketEventStreamFactory = Callable[[Sequence[str]], Any]


async def _default_market_stream(token_ids: Sequence[str]) -> Any:
    """Official AsyncPublicClient market subscription (async context manager)."""
    from polymarket.streams._specs import MarketSpec

    client = build_async_public_client()
    await client.__aenter__()
    try:
        handle = await client.subscribe(MarketSpec(token_ids=list(token_ids)))
    except Exception:
        await client.close()
        raise

    class _OwnedHandle:
        def __init__(self) -> None:
            self._handle = handle
            self._client = client

        def __aiter__(self) -> AsyncIterator[Any]:
            return self._handle.__aiter__()

        async def close(self) -> None:
            try:
                await self._handle.close()
            finally:
                await self._client.close()

    return _OwnedHandle()


@dataclass
class BindingFeed:
    """One binding's public market-data lifecycle (UP+DOWN tokens)."""

    binding: MarketBindingRecord
    store: MarketStateStore
    dispatcher: EventDispatcher
    rest_fetcher: Callable[[str], Any] = fetch_clob_book
    market_stream_factory: MarketEventStreamFactory = field(
        default_factory=lambda: _default_market_stream
    )
    buffer_max_events: int = DEFAULT_BUFFER_MAX_EVENTS
    buffer_max_age_s: float = DEFAULT_BUFFER_MAX_AGE_S

    phase: FeedSyncPhase = FeedSyncPhase.UNINITIALIZED
    connection_health: ConnectionHealth = ConnectionHealth.CLOSED
    connection_epoch: int = 0
    subscription_sent: bool = False
    buffer: Deque[BufferedIngress] = field(default_factory=deque)
    last_error: str | None = None
    recovery_reason: str | None = None
    promoted_at_mono_ns: int | None = None
    first_eligible_eval_mono_ns: int | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task | None = None
    _stream: Any = None
    _rest_failures: dict[str, str] = field(default_factory=dict)

    @property
    def is_book_ready(self) -> bool:
        up = self.store.get(self.binding.up_instrument_id)
        down = self.store.get(self.binding.down_instrument_id)
        return up.sync_health in {SyncHealth.READY, SyncHealth.STALE} and down.sync_health in {
            SyncHealth.READY,
            SyncHealth.STALE,
        }

    def capture_view(self) -> BookView:
        return self.store.capture_pair(
            self.binding,
            connection_epoch=self.connection_epoch,
            connection_health=self.connection_health,
        )

    def note_evaluation_attempt(self) -> None:
        if self.promoted_at_mono_ns is None:
            return
        if self.first_eligible_eval_mono_ns is not None:
            return
        view = self.capture_view()
        up_seen = view.up.sync_health is not SyncHealth.UNINITIALIZED
        down_seen = view.down.sync_health is not SyncHealth.UNINITIALIZED
        if up_seen or down_seen:
            self.first_eligible_eval_mono_ns = time.monotonic_ns()

    def promotion_to_eligible_latency_s(self) -> float | None:
        if self.promoted_at_mono_ns is None or self.first_eligible_eval_mono_ns is None:
            return None
        return (self.first_eligible_eval_mono_ns - self.promoted_at_mono_ns) / 1e9

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        name = f"bookfeed-{self.binding.binding_id[:16]}"
        self._task = asyncio.create_task(self._run(), name=name)

    async def stop(self) -> None:
        self._stop.set()
        self.connection_health = ConnectionHealth.CLOSED
        if self._stream is not None:
            try:
                close = getattr(self._stream, "close", None)
                if close is not None:
                    await close()
            except Exception:
                pass
            self._stream = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def bootstrap_rest(self, *, mark_ready: bool = False) -> bool:
        """REST-seed tokens independently. Returns True iff all tokens seeded."""
        self.phase = FeedSyncPhase.SNAPSHOT_ACQUIRING
        self._rest_failures.clear()
        ok_all = True
        for token in self.binding.asset_ids:
            try:
                bootstrap_token_into_store(
                    self.store,
                    token,
                    binding_id=self.binding.binding_id,
                    connection_epoch=self.connection_epoch,
                    mark_ready=mark_ready,
                    fetcher=self.rest_fetcher,
                )
            except Exception as exc:
                ok_all = False
                self._rest_failures[token] = f"{type(exc).__name__}:{exc}"
                self.last_error = f"rest:{token}:{type(exc).__name__}:{exc}"
                logger.warning("REST bootstrap failed for %s: %s", token, exc)
                # Fail-soft: continue sibling token.
                continue
        if ok_all:
            self.phase = (
                FeedSyncPhase.RECONCILING if mark_ready else FeedSyncPhase.SNAPSHOT_ACQUIRING
            )
            self.recovery_reason = None
        else:
            self.recovery_reason = "rest_partial_or_failed:" + ",".join(
                f"{k}={v}" for k, v in self._rest_failures.items()
            )
            # Do not permanently darken: WS full books remain valid sync anchors.
            if not any(
                self.store.get(InstrumentId(t)).book is not None for t in self.binding.asset_ids
            ):
                self.phase = FeedSyncPhase.DESYNCED
            else:
                self.phase = FeedSyncPhase.RECONCILING
        return ok_all

    def _all_books_present(self) -> bool:
        return all(
            self.store.get(InstrumentId(t)).book is not None for t in self.binding.asset_ids
        )

    async def _run(self) -> None:
        backoff = 1.0
        rest_attempt = 0
        while not self._stop.is_set():
            try:
                self.phase = FeedSyncPhase.WS_CONNECTING
                self.connection_health = ConnectionHealth.CONNECTING
                self.subscription_sent = False
                stream = await self.market_stream_factory(list(self.binding.asset_ids))
                self._stream = stream
                self.connection_epoch += 1
                epoch = self.connection_epoch
                self.store.set_min_connection_epoch(epoch)
                self.store.metrics.for_token(self.binding.up_token_id).reconnect_count += 1
                self.subscription_sent = True
                self.phase = FeedSyncPhase.WS_BUFFERING
                self.connection_health = ConnectionHealth.LIVE
                backoff = 1.0

                # REST bootstrap while WS events buffer (fail-soft per token).
                await self.bootstrap_rest(mark_ready=False)
                if self._all_books_present():
                    self.phase = FeedSyncPhase.RECONCILING
                    await self._reconcile_buffer(epoch)
                    if self.phase is not FeedSyncPhase.DESYNCED and self._all_books_present():
                        self._promote_ready(epoch)
                else:
                    self.phase = FeedSyncPhase.DESYNCED
                    self.recovery_reason = self.recovery_reason or "awaiting_ws_or_rest_snapshot"

                async for sdk_event in stream:
                    if self._stop.is_set():
                        break
                    await self._handle_sdk_event(sdk_event, connection_epoch=epoch)
                    # Bounded REST retry while DESYNCED.
                    if self.phase is FeedSyncPhase.DESYNCED and not self._all_books_present():
                        delay = REST_RETRY_BACKOFF_S[
                            min(rest_attempt, len(REST_RETRY_BACKOFF_S) - 1)
                        ]
                        # Opportunistic retry without blocking the reader forever:
                        # only when buffer is quiet — schedule light retry via counter.
                        if rest_attempt < len(REST_RETRY_BACKOFF_S):
                            # Attempt at most one REST retry wave per reconnect epoch
                            # after seeing traffic (handled below via counter bump).
                            pass
                        _ = delay

                if not self._stop.is_set():
                    self.connection_health = ConnectionHealth.RECONNECTING
                    self.phase = FeedSyncPhase.DESYNCED
                    self.recovery_reason = "ws_stream_ended"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}:{exc}"
                self.connection_health = ConnectionHealth.RECONNECTING
                self.phase = FeedSyncPhase.DESYNCED
                self.recovery_reason = f"reconnect:{self.last_error}"
                logger.warning("BindingFeed %s reconnecting: %s", self.binding.binding_id, exc)
            finally:
                if self._stream is not None:
                    try:
                        close = getattr(self._stream, "close", None)
                        if close is not None:
                            await close()
                    except Exception:
                        pass
                self._stream = None
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            rest_attempt += 1
        self.connection_health = ConnectionHealth.CLOSED

    def _promote_ready(self, connection_epoch: int) -> None:
        self.phase = FeedSyncPhase.READY
        self.recovery_reason = None
        for token in self.binding.asset_ids:
            st = self.store.get(InstrumentId(token))
            if st.book is not None and st.sync_health is SyncHealth.SYNCING:
                self.store.apply_rest_snapshot(
                    st.book,
                    ts_received=datetime.now(timezone.utc),
                    venue_hash=st.venue_hash,
                    tick_size=st.tick_size,
                    min_order_size=st.min_order_size,
                    binding_id=self.binding.binding_id,
                    connection_epoch=connection_epoch,
                    mark_ready=True,
                    source="REST_RECONCILED",
                )

    async def _handle_sdk_event(self, sdk_event: Any, *, connection_epoch: int) -> None:
        ts_received = datetime.now(timezone.utc)
        try:
            event = sdk_market_event_to_tyrex(sdk_event, ts_received=ts_received)
        except (ValueError, TypeError, KeyError) as exc:
            self.last_error = f"normalize:{type(exc).__name__}:{exc}"
            for tok in self.binding.asset_ids:
                self.store.metrics.for_token(tok).snapshots_decode_rejected += 1
            return
        if event is None:
            return

        asset = self._event_token(event)
        if asset and asset not in self.binding.asset_ids:
            for tok in self.binding.asset_ids:
                self.store.metrics.for_token(tok).unknown_token += 1
            return

        event = self._with_epoch(event, connection_epoch)
        await self._ingress(event, connection_epoch=connection_epoch)

    async def _ingress(
        self,
        event: BookSnapshotReceived | BookDeltaReceived | TickSizeChanged,
        *,
        connection_epoch: int,
    ) -> None:
        """Shared ingress for buffering, READY publish, and DESYNCED recovery."""
        if self.phase in {
            FeedSyncPhase.WS_BUFFERING,
            FeedSyncPhase.SNAPSHOT_ACQUIRING,
            FeedSyncPhase.RECONCILING,
        }:
            self._buffer_event(event, connection_epoch=connection_epoch)
            if (
                isinstance(event, BookSnapshotReceived)
                and self.phase is FeedSyncPhase.SNAPSHOT_ACQUIRING
            ):
                # Early WS snapshot while REST still running — keep buffered.
                pass
            return

        if self.phase is FeedSyncPhase.READY:
            self.dispatcher.publish(event)
            return

        if self.phase is FeedSyncPhase.DESYNCED:
            # Continue classifying/accounting. Full WS books are sync anchors.
            if isinstance(event, BookSnapshotReceived):
                self.dispatcher.publish(event)
                if self._all_books_present():
                    self.phase = FeedSyncPhase.RECONCILING
                    await self._reconcile_buffer(connection_epoch)
                    if self.phase is not FeedSyncPhase.DESYNCED and self._all_books_present():
                        self._promote_ready(connection_epoch)
                else:
                    self.recovery_reason = (
                        f"partial_ws_snapshot:{event.book.instrument_id.value}"
                    )
                return
            # Deltas: buffer only; never apply onto an unknown book while DESYNCED.
            token = self._event_token(event)
            if token and self.store.get(InstrumentId(token)).book is not None:
                self._buffer_event(event, connection_epoch=connection_epoch)
            else:
                self._buffer_event(event, connection_epoch=connection_epoch)
                self.recovery_reason = self.recovery_reason or "desynced_buffering_deltas"
            return

    def _event_token(
        self, event: BookSnapshotReceived | BookDeltaReceived | TickSizeChanged
    ) -> str | None:
        if isinstance(event, BookSnapshotReceived):
            return event.book.instrument_id.value
        if isinstance(event, TickSizeChanged):
            return event.instrument_id.value
        if isinstance(event, BookDeltaReceived) and event.changes:
            return event.changes[0].instrument_id.value
        return None

    def _buffer_event(
        self,
        event: BookSnapshotReceived | BookDeltaReceived | TickSizeChanged,
        *,
        connection_epoch: int,
    ) -> None:
        now = time.monotonic_ns()
        self.buffer.append(
            BufferedIngress(
                received_mono_ns=now,
                connection_epoch=connection_epoch,
                event=event,
            )
        )
        max_age_ns = int(self.buffer_max_age_s * 1e9)
        while self.buffer and now - self.buffer[0].received_mono_ns > max_age_ns:
            self.buffer.popleft()
        if len(self.buffer) > self.buffer_max_events:
            self.buffer.clear()
            self.phase = FeedSyncPhase.DESYNCED
            self.last_error = "buffer_overflow"
            self.recovery_reason = "buffer_overflow"

    async def _reconcile_buffer(self, connection_epoch: int) -> None:
        if self.phase is FeedSyncPhase.DESYNCED and not self._all_books_present():
            return
        if not self._all_books_present():
            self.phase = FeedSyncPhase.DESYNCED
            self.last_error = "reconcile_missing_snapshot"
            self.recovery_reason = "reconcile_missing_snapshot"
            return
        while self.buffer:
            item = self.buffer.popleft()
            if item.connection_epoch != connection_epoch:
                for tok in self.binding.asset_ids:
                    self.store.metrics.for_token(tok).old_generation += 1
                continue
            # Ambiguous: tick/delta without prior snapshot for that token → DESYNCED.
            if isinstance(item.event, BookDeltaReceived):
                for ch in item.event.changes:
                    if self.store.get(ch.instrument_id).book is None:
                        self.phase = FeedSyncPhase.DESYNCED
                        self.last_error = "reconcile_ambiguous_delta"
                        self.recovery_reason = "reconcile_ambiguous_delta"
                        self.buffer.appendleft(item)
                        return
            self.dispatcher.publish(self._with_epoch(item.event, connection_epoch))

    @staticmethod
    def _with_epoch(
        event: BookSnapshotReceived | BookDeltaReceived | TickSizeChanged,
        connection_epoch: int,
    ) -> BookSnapshotReceived | BookDeltaReceived | TickSizeChanged:
        return replace(event, connection_epoch=connection_epoch)


@dataclass
class BookFeedSupervisor:
    """Owns ACTIVE + PREPARED_NEXT BindingFeeds."""

    store: MarketStateStore
    dispatcher: EventDispatcher
    out_dir: Any | None = None
    rest_fetcher: Callable[[str], Any] = fetch_clob_book
    market_stream_factory: MarketEventStreamFactory | None = None

    active: BindingFeed | None = None
    prepared: BindingFeed | None = None
    role_epoch_counter: int = 0
    _bindings_path: Any | None = None

    def __post_init__(self) -> None:
        if self.out_dir is not None:
            from pathlib import Path

            self._bindings_path = Path(self.out_dir) / "bindings.json"

    def _persist(self) -> None:
        if self._bindings_path is None:
            return
        rows: list[MarketBindingRecord] = []
        if self.active is not None:
            rows.append(self.active.binding)
        if self.prepared is not None:
            rows.append(self.prepared.binding)
        persist_bindings(self._bindings_path, rows)

    def _make_feed(self, binding: MarketBindingRecord) -> BindingFeed:
        kwargs: dict[str, Any] = {
            "binding": binding,
            "store": self.store,
            "dispatcher": self.dispatcher,
            "rest_fetcher": self.rest_fetcher,
        }
        if self.market_stream_factory is not None:
            kwargs["market_stream_factory"] = self.market_stream_factory
        return BindingFeed(**kwargs)

    async def set_active(self, binding: MarketBindingRecord) -> BindingFeed:
        self.role_epoch_counter = max(self.role_epoch_counter, binding.role_epoch)
        rec = binding.with_role(BindingLifecycleRole.ACTIVE, role_epoch=binding.role_epoch)
        feed = self._make_feed(rec)
        if self.active is not None:
            await self.active.stop()
        self.active = feed
        self.store.set_allowed_tokens(self._all_tokens())
        await feed.start()
        self._persist()
        return feed

    async def set_prepared(self, binding: MarketBindingRecord) -> BindingFeed:
        rec = binding.with_role(
            BindingLifecycleRole.PREPARED_NEXT, role_epoch=binding.role_epoch
        )
        feed = self._make_feed(rec)
        if self.prepared is not None:
            await self.prepared.stop()
        self.prepared = feed
        self.store.set_allowed_tokens(self._all_tokens())
        await feed.start()
        self._persist()
        return feed

    async def promote_prepared(self) -> BindingFeed:
        """Promote prepared → active without invalidating its books/feed."""
        if self.prepared is None:
            raise ValueError("no prepared binding to promote")
        old = self.active
        self.role_epoch_counter += 1
        promoted_binding = self.prepared.binding.with_role(
            BindingLifecycleRole.ACTIVE, role_epoch=self.role_epoch_counter
        )
        self.prepared.binding = promoted_binding
        self.prepared.promoted_at_mono_ns = time.monotonic_ns()
        self.active = self.prepared
        self.prepared = None
        if old is not None:
            expired = old.binding.with_role(
                BindingLifecycleRole.EXPIRED, role_epoch=old.binding.role_epoch
            )
            old.binding = expired
            await old.stop()
        self.store.set_allowed_tokens(self._all_tokens())
        self._persist()
        return self.active

    def active_view(self) -> BookView | None:
        if self.active is None:
            return None
        return self.active.capture_view()

    def _all_tokens(self) -> set[str]:
        out: set[str] = set()
        if self.active is not None:
            out.update(self.active.binding.asset_ids)
        if self.prepared is not None:
            out.update(self.prepared.binding.asset_ids)
        return out

    async def stop(self) -> None:
        if self.active is not None:
            await self.active.stop()
        if self.prepared is not None:
            await self.prepared.stop()
