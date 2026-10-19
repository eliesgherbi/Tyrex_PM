"""Recorded warehouse rows → the live runtime's normalized inputs.

The strategy side of a replay is the real ``MarketSessionRuntime`` + ``MarketStateStore``
+ ``IndicatorGraph`` stack, fed through the shared router. Only the transport differs:

* Reference ticks become ``SettlementReferenceUpdated`` / ``ReferencePriceUpdated``
  events routed by ``runtime.market_event_router`` (one indicator update per trade).
* Books are reconstructed by the warehouse ``Books`` (per-socket streams, gap and
  crossed-book invalidation, as used by quality reports and the replay engine) and
  published to the store as full snapshots, or invalidated when the stream is unusable.
* K is sealed without attestation, exactly like the live no-SSR path; its quality is
  therefore PROVISIONAL and recorded as such.

The reference series replayed here is the warehouse PolyBolt TWAP feed. The live
runtime consumes RTDS; their identity is an open S0 question, so results name it.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tyrex_pm.core.book_events import BookSnapshotReceived, TickSizeChanged
from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.events import EventSource, ReferencePriceUpdated, SettlementReferenceUpdated
from tyrex_pm.core.ids import CorrelationId, EventId, InstrumentId, MarketId, TokenId
from tyrex_pm.core.ingress import FeedRole, IngressMeta
from tyrex_pm.core.instruments import Instrument, OutcomeSide
from tyrex_pm.core.snapshots import (
    BookLevel,
    BookSnapshot,
    ReferencePriceSnapshot,
    SettlementReferenceSnapshot,
)
from tyrex_pm.data_warehouse.contracts import decode
from tyrex_pm.data_warehouse.reconstruction.books import Books
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.domain.polymarket.reference_availability import (
    DELIVERY_LIVE,
    ReferenceAvailabilityPolicy,
    is_history,
    selected_boundary_tick,
)
from tyrex_pm.facts import ids as F
from tyrex_pm.market_data.binding_record import (
    BindingLifecycleRole,
    MarketBindingRecord,
    make_binding_id,
)
from tyrex_pm.market_data.book_store import MarketStateStore
from tyrex_pm.runtime.market_event_router import (
    route_boundary_history,
    route_settlement_reference,
    route_trading_reference,
)
from tyrex_pm.runtime.market_runtime import MarketSessionRuntime, SessionSlot

from .loader import NS, WindowData

REPLAYED_REFERENCE_SERIES = "polybolt:price.crypto.twap:chainlink:btcusd:twap:60"
# Captured live runs replay the RTDS series the live runtime consumed (capture_replay).
RTDS_REFERENCE_SERIES = "polymarket_rtds:crypto_prices_twap_sixty:btc/usd"


def reference_series_of(row: dict) -> str:
    return RTDS_REFERENCE_SERIES if row.get("source_id") == "rtds" else (
        REPLAYED_REFERENCE_SERIES)


def utc(ns: int) -> datetime:
    return datetime.fromtimestamp(ns // NS, tz=timezone.utc).replace(
        microsecond=(ns % NS) // 1_000
    )


class PointClock:
    """Store-only clock set explicitly to recorded instants (may move back to a row's
    receive time during ``flush``; the store reads time only when applying or capturing).
    """

    def __init__(self, ns: int) -> None:
        self._ns = int(ns)

    def at(self, ns: int) -> None:
        self._ns = int(ns)

    def now_utc(self) -> datetime:
        return utc(self._ns)

    def monotonic_ns(self) -> int:
        return self._ns


def order_key(row: dict) -> tuple:
    return (row["received_at"], row.get("session_id") or "", row.get("ingest_sequence") or 0)


def _event_ids(row: dict) -> dict:
    record = str(row.get("record_id") or "")
    return {
        "event_id": EventId(f"replay:{record[:32]}"),
        "correlation_id": CorrelationId(f"replay:{record[:32]}"),
    }


def build_market(window: WindowData) -> BinaryMarket:
    market_id = MarketId(window.ref.market_id)
    yes = Instrument(
        instrument_id=InstrumentId(window.up_token_id),
        market_id=market_id,
        token_id=TokenId(window.up_token_id),
        outcome=OutcomeSide.YES,
        symbol="UP",
    )
    no = Instrument(
        instrument_id=InstrumentId(window.down_token_id),
        market_id=market_id,
        token_id=TokenId(window.down_token_id),
        outcome=OutcomeSide.NO,
        symbol="DOWN",
    )
    return BinaryMarket(
        market_id=market_id,
        condition_id=window.condition_id,
        question=window.question,
        yes=yes,
        no=no,
        event_start=utc(window.ref.window_start_ns),
        event_end=utc(window.ref.window_end_ns),
        event_slug=window.ref.slug,
    )


@dataclass
class StrategyFeed:
    """Applies the strategy-visible rows (already delayed by the timeline) in order."""

    window: WindowData
    runtime: MarketSessionRuntime
    store: MarketStateStore
    clock: FakeClock
    binding: MarketBindingRecord
    books: Books = field(default_factory=lambda: Books(retain_epochs=False))
    _sequence: int = 0
    _generations: dict[str, int] = field(default_factory=dict)
    seals: list[dict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    # Levels per side published to the store; must be >= the strategy's level bound.
    depth_levels: int = 64
    store_clock: "PointClock" = field(default_factory=lambda: PointClock(0))
    # Latest recorded clock offset (collector host -> reference clock); zero when uncorrected.
    clock_offset: timedelta = field(default_factory=timedelta)
    reference_policy: ReferenceAvailabilityPolicy = field(
        default_factory=ReferenceAvailabilityPolicy.legacy
    )
    _arrival_refused: set[str] = field(default_factory=set)
    # Series of the reference rows replayed (PolyBolt warehouse or captured RTDS).
    reference_series: str = REPLAYED_REFERENCE_SERIES
    # True publishes every book change immediately (reference mode for equivalence tests).
    eager_books: bool = False
    _pending: dict[str, tuple[str, dict, bool]] = field(default_factory=dict)
    _served: dict[str, bool] = field(default_factory=dict)
    _level_cache: dict[tuple[Decimal, Decimal], BookLevel] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        window: WindowData,
        runtime: MarketSessionRuntime,
        clock: FakeClock,
        *,
        depth_levels: int = 64,
    ):
        market = build_market(window)
        store_clock = PointClock(clock.monotonic_ns())
        store = MarketStateStore(clock=store_clock)
        binding = MarketBindingRecord(
            binding_id=make_binding_id(
                window_slug=window.ref.slug, condition_id=window.condition_id
            ),
            window_slug=window.ref.slug,
            condition_id=window.condition_id,
            market_id=window.ref.market_id,
            up_token_id=window.up_token_id,
            down_token_id=window.down_token_id,
            event_start=market.event_start,
            event_end=market.event_end,
            role=BindingLifecycleRole.ACTIVE,
            role_epoch=0,
            outcome_semantics="UP_DOWN",
            resolved_at=market.event_start,
        )
        runtime.book_store = store
        runtime.active_binding_record = binding
        runtime.open_session(
            slot=SessionSlot.ACTIVE, market=market, window_id=window.ref.slug
        )
        return cls(
            window=window,
            runtime=runtime,
            store=store,
            clock=clock,
            binding=binding,
            depth_levels=depth_levels,
            store_clock=store_clock,
        )

    # -- helpers -------------------------------------------------------------------

    def _ingress(self, row: dict, role: FeedRole) -> IngressMeta:
        self._sequence += 1
        connection = str(row.get("connection_id") or row.get("session_id") or "unknown")
        generation = self._generations.setdefault(connection, len(self._generations) + 1)
        received = utc(row["received_at"])
        return IngressMeta(
            receive_monotonic_ns=int(row["received_at"]),
            ingress_sequence=self._sequence,
            connection_generation=generation,
            receive_wall_raw_utc=received,
            receive_wall_corrected_utc=received + self.clock_offset,
            clock_offset_ms=self.clock_offset.total_seconds() * 1000.0,
            clock_uncertainty_ms=0,
            clock_status="REPLAY_RECORDED_RECEIVE_TIME",
            raw_fingerprint=str(row.get("record_id") or ""),
            role=role,
            delivery_kind=str(row.get("delivery_kind") or DELIVERY_LIVE),
        )

    def _bump(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    # -- application ---------------------------------------------------------------

    def apply(self, dataset: str, row: dict) -> str | None:
        """Apply one strategy-visible row; returns the fact it updated, if any."""
        if dataset in ("book_snapshots", "book_updates", "operational_events"):
            return self._apply_book(dataset, row)
        if dataset == "trade_prints" and row.get("source_id") == "binance":
            return self._apply_trade(row)
        if dataset == "reference_observations":
            return self._apply_reference(row)
        if dataset == "market_changes" and row.get("change_type") == "tick_size_change":
            return self._apply_tick_change(row)
        return None

    def _apply_tick_change(self, row: dict) -> str | None:
        """Live semantics: the store records the new tick and marks the token DESYNCED
        until its next snapshot; pending book state before the change is applied first."""
        token = self.window.token_by_instrument.get(row.get("entity_id") or "")
        if token is None:
            return None
        value = decode(row.get("new_value") or "{}") or {}
        if "new_tick_size" not in value or "old_tick_size" not in value:
            return None
        self.flush(row["received_at"])
        at = utc(row["received_at"])
        self.store.on_tick_size(
            TickSizeChanged(
                **_event_ids(row),
                ts_event=utc(row.get("source_time") or row["received_at"]),
                ts_received=at,
                source=EventSource.POLYMARKET_CLOB,
                instrument_id=InstrumentId(token),
                old_tick_size=Decimal(str(value["old_tick_size"])),
                new_tick_size=Decimal(str(value["new_tick_size"])),
                market_id=MarketId(self.window.ref.market_id),
            )
        )
        self._served[token] = False
        self._bump("tick_changes")
        return F.POLYMARKET_BOOKS

    def _apply_book(self, dataset: str, row: dict) -> str | None:
        """Reconstruct, then record the store action this row implies (applied lazily).

        The store is only read when a decision captures the book pair, so publication
        is deferred to ``flush`` with exact timing: the store's own clock is set to the
        recorded receive time of the row that produced each published state, so apply
        times, staleness and data ages are identical to publishing every row.
        """
        self.books.apply(dataset, row)
        if dataset == "book_snapshots":
            affected = {row.get("instrument_id")}
        elif dataset == "book_updates":
            affected = {c.get("instrument_id") for c in row.get("changes", [])}
        else:
            affected = set(self.window.token_by_instrument)
        touched = False
        for wh_iid, token in self.window.token_by_instrument.items():
            if wh_iid not in affected:
                continue
            book = self.books.books.get(wh_iid)
            pending = self._pending.get(token)
            if book is not None and book.valid and dataset != "operational_events":
                self._pending[token] = ("publish", row, pending is not None and pending[2])
                touched = True
            elif book is None or not book.valid:
                served = self._served.get(token, False)
                superseded = pending is not None and pending[0] == "publish"
                if pending is not None and pending[0] == "invalidate":
                    continue  # Keep the first invalidating row's time.
                if served or superseded:
                    # Unusable stream (gap, crossed book, disconnect): stop serving it.
                    self._pending[token] = ("invalidate", row, superseded)
                    touched = True
        if touched:
            self._bump("book_events")
            if self.eager_books:
                self.flush(row["received_at"])
            return F.POLYMARKET_BOOKS
        return None

    def flush(self, now_ns: int) -> None:
        """Apply pending store actions in recorded order, then bring the store to now."""
        for token, (action, row, _) in sorted(
            self._pending.items(), key=lambda item: order_key(item[1][1])
        ):
            self.store_clock.at(row["received_at"])
            if action == "publish":
                book = self.books.books.get(self.window.instrument_by_token[token])
                if book is not None:
                    self._publish(row, token, book)
                    self._served[token] = True
            else:
                self.store.invalidate(
                    InstrumentId(token),
                    ts_event=utc(row["received_at"]),
                    ts_received=utc(row["received_at"]),
                )
                self._served[token] = False
            self._bump("book_publications")
        self._pending.clear()
        self.store_clock.at(now_ns)

    def _level(self, price: Decimal, quantity: Decimal) -> BookLevel:
        # Most levels are unchanged between consecutive updates; validated BookLevel
        # objects are immutable, so they are reused instead of rebuilt (bounded cache).
        key = (price, quantity)
        level = self._level_cache.get(key)
        if level is None:
            if len(self._level_cache) > 200_000:
                self._level_cache.clear()
            level = self._level_cache[key] = BookLevel(price=price, quantity=quantity)
        return level

    def _publish(self, row: dict, token: str, book) -> None:
        snapshot = BookSnapshot(
            instrument_id=InstrumentId(token),
            ts_event=utc(row.get("source_time") or row["received_at"]),
            bids=tuple(
                self._level(p, q)
                for p, q in heapq.nlargest(self.depth_levels, book.bids.items())
            ),
            asks=tuple(
                self._level(p, q)
                for p, q in heapq.nsmallest(self.depth_levels, book.asks.items())
            ),
        )
        self.store.on_snapshot(
            BookSnapshotReceived(
                **_event_ids(row),
                ts_event=snapshot.ts_event,
                ts_received=utc(row["received_at"]),
                source=EventSource.POLYMARKET_CLOB,
                book=snapshot,
                market_id=MarketId(self.window.ref.market_id),
            )
        )

    def seed_constraints(self, tick: Decimal | None, minimum: Decimal | None) -> None:
        for token in (self.window.up_token_id, self.window.down_token_id):
            self.store.seed_trading_parameters(
                token, tick_size=tick, min_order_size=minimum, binding_id=self.binding.binding_id
            )

    def _apply_trade(self, row: dict) -> str:
        event = ReferencePriceUpdated(
            **_event_ids(row),
            ts_event=utc(row.get("source_time") or row["received_at"]),
            ts_received=utc(row["received_at"]),
            source=EventSource.BINANCE,
            reference=ReferencePriceSnapshot(
                symbol="BTCUSDT",
                price=row["price"],
                ts_event=utc(row.get("source_time") or row["received_at"]),
                venue="binance",
            ),
            ingress=self._ingress(row, FeedRole.TRADING_REFERENCE),
        )
        route_trading_reference(self.runtime, event)
        self._bump("binance_trades")
        return F.BINANCE_SPOT_TRADES

    def _apply_reference(self, row: dict) -> str | None:
        if row.get("measurement_type") != "TWAP" or row.get("value") is None:
            return None
        source_ts = utc(row.get("source_time") or row["received_at"])
        event = SettlementReferenceUpdated(
            **_event_ids(row),
            ts_event=source_ts,
            ts_received=utc(row["received_at"]),
            source=EventSource.SYSTEM,
            settlement=SettlementReferenceSnapshot(
                symbol="BTC/USD",
                price=row["value"],
                ts_event=source_ts,
                venue="polybolt_replay" if row.get("source_id") != "rtds"
                else "polymarket_rtds_twap_sixty",
                provider="chainlink",
            ),
            ingress=self._ingress(row, FeedRole.SETTLEMENT_REFERENCE),
        )
        self.reference_series = reference_series_of(row)
        policy = self.reference_policy
        if not policy.is_legacy and is_history(event.ingress):
            # Same rule as live: history only recovers an exact boundary tick, at its
            # recorded arrival; it is never the latest reference nor a fact update.
            for window_id in route_boundary_history(self.runtime, event, policy):
                self._bump("reference_boundary_recovered_from_history")
                self._seal_without_attestation(window_id)
            self._bump("reference_history_ticks")
            return None
        for window_id in route_settlement_reference(self.runtime, event):
            self._seal_without_attestation(window_id)
        self._bump("reference_ticks")
        return F.CHAINLINK_TWAP

    def _seal_without_attestation(self, window_id: str) -> None:
        """Mirror of the live no-SSR seal: seal once an EXACT candidate exists."""
        sess = self.runtime.active
        if sess is None or sess.window_id != window_id or sess.sealed is not None:
            return
        state = self.runtime.ptb_engine.get_window(sess.market_id, window_id)
        if state is None or state.selected_candidate is None:
            return
        if window_id in self._arrival_refused:
            return
        policy = self.reference_policy
        boundary = selected_boundary_tick(state)
        recovered = is_history(None if boundary is None else boundary.ingress)
        lag_ms = (
            None
            if boundary is None
            else (boundary.receive_wall_raw_utc - state.event_start).total_seconds() * 1000.0
        )
        if not policy.arrival_ok(lag_ms=lag_ms, recovered_from_history=recovered):
            # Live seals such a window with ptb_ready false (entries blocked for the
            # window); replay leaves it unsealed for the same effect.
            self._arrival_refused.add(window_id)
            self.seals.append(
                {
                    "window_id": window_id,
                    "refused": "ptb_lag_exceeded",
                    "boundary_lag_ms": lag_ms,
                    "bound_ms": policy.bound_ms(recovered_from_history=recovered),
                    "recovered_from_history": recovered,
                    "availability_policy": policy.policy_id,
                }
            )
            return
        sealed = self.runtime.attest_and_seal(
            market_id=sess.market_id,
            window_id=window_id,
            sealed_at=self.clock.now_utc(),
            require_attestation_match=False,
            skip_attestation=True,
        )
        snap = state.ptb_snapshot
        self.seals.append(
            {
                "window_id": window_id,
                "k": str(sealed.ptb_k),
                "boundary_rule": sealed.boundary_rule_id.value,
                "boundary_source_ts": sealed.chainlink_boundary_source_ts.isoformat(),
                "boundary_lag_ms": sealed.boundary_lag_ms,
                "sealed_at": sealed.sealed_at.isoformat(),
                "quality": None if snap is None else snap.quality.value,
                "attestation": "SKIPPED_NO_ATTESTATION_SOURCE_IN_REPLAY",
                "reference_series": self.reference_series,
                "recovered_from_history": recovered,
                "availability_policy": policy.policy_id,
            }
        )
