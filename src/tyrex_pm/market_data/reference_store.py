"""Authoritative trading/comparison reference price state (Binance / RTDS Binance).

Never treats these series as settlement / Chainlink truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from tyrex_pm.core.events import EventSource, ReferencePriceUpdated
from tyrex_pm.core.ingress import (
    AppendOnlyIngressLog,
    FeedReadiness,
    FeedRole,
    IngressRecord,
)
from tyrex_pm.core.snapshots import ReferencePriceSnapshot
from tyrex_pm.engine.dispatcher import EventDispatcher


@dataclass
class ReferenceState:
    snapshot: ReferencePriceSnapshot | None = None
    initialized: bool = False
    last_ts_event: datetime | None = None
    last_ts_received: datetime | None = None
    last_connection_generation: int | None = None
    readiness: FeedReadiness = FeedReadiness.NOT_READY
    venue: str | None = None
    source: EventSource | None = None


@dataclass
class ReferenceDataStore:
    """Current-view store for trading/comparison references."""

    ingress: AppendOnlyIngressLog = field(default_factory=AppendOnlyIngressLog)
    # Keyed by (venue, symbol) so Binance Spot and RTDS Binance do not overwrite
    _by_key: dict[tuple[str, str], ReferenceState] = field(default_factory=dict)
    # Legacy single-series view (direct Binance preferred)
    _by_symbol: dict[str, ReferenceState] = field(default_factory=dict)

    def attach(self, dispatcher: EventDispatcher) -> None:
        dispatcher.subscribe(ReferencePriceUpdated, self.on_reference)

    def get(self, symbol: str) -> ReferenceState:
        return self._by_symbol.get(symbol.upper(), ReferenceState())

    def get_series(self, *, venue: str, symbol: str) -> ReferenceState:
        return self._by_key.get((venue.lower(), symbol.upper()), ReferenceState())

    def mark_not_ready(self, symbol: str, *, venue: str | None = None) -> None:
        if venue is not None:
            key = (venue.lower(), symbol.upper())
            state = self._by_key.get(key, ReferenceState())
            state.readiness = FeedReadiness.NOT_READY
            state.initialized = False
            self._by_key[key] = state
        state = self._by_symbol.get(symbol.upper())
        if state is not None and (venue is None or state.venue == venue):
            state.readiness = FeedReadiness.NOT_READY
            state.initialized = False

    def on_reference(self, event: ReferencePriceUpdated) -> None:
        symbol = event.reference.symbol.upper()
        venue = (event.reference.venue or "unknown").lower()
        key = (venue, symbol)
        state = self._by_key.get(key, ReferenceState())
        accepted = True
        reject_reason = None
        # Source-specific dedupe: Binance trade IDs — identical provider seq may
        # still be distinct stream deliveries; do not drop solely on (ts, value).
        if state.last_ts_event is not None and event.ts_event < state.last_ts_event:
            accepted = False
            reject_reason = "stale_source_ts"

        role = FeedRole.TRADING_REFERENCE
        if event.source is EventSource.POLYMARKET_RTDS_BINANCE:
            role = FeedRole.COMPARISON_REFERENCE
        meta = event.ingress
        if meta is not None:
            raw_wall = meta.receive_wall_raw_utc or event.ts_received
            self.ingress.append(
                IngressRecord(
                    source=event.source.value,
                    symbol=symbol,
                    role=role,
                    source_ts=event.ts_event,
                    receive_wall_utc=raw_wall,
                    value=str(event.reference.price),
                    meta=meta,
                    accepted_by_current_view=accepted,
                    reject_reason=reject_reason,
                )
            )
        if not accepted:
            return

        gen = meta.connection_generation if meta else None
        new_state = ReferenceState(
            snapshot=event.reference,
            initialized=True,
            last_ts_event=event.ts_event,
            last_ts_received=event.ts_received,
            last_connection_generation=gen,
            readiness=FeedReadiness.READY,
            venue=venue,
            source=event.source,
        )
        self._by_key[key] = new_state
        # Primary trading view: direct Binance Spot only (never RTDS Binance)
        if venue == "binance" or event.source is EventSource.BINANCE:
            self._by_symbol[symbol] = new_state
