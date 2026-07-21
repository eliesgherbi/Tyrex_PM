"""Settlement-reference store (RTDS Chainlink) — distinct from Binance trading ref."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from tyrex_pm.core.events import SettlementReferenceUpdated
from tyrex_pm.core.ingress import (
    AppendOnlyIngressLog,
    FeedReadiness,
    FeedRole,
    IngressRecord,
)
from tyrex_pm.core.snapshots import SettlementReferenceSnapshot
from tyrex_pm.engine.dispatcher import EventDispatcher


@dataclass
class SettlementState:
    snapshot: SettlementReferenceSnapshot | None = None
    initialized: bool = False
    last_ts_event: datetime | None = None
    last_ts_received: datetime | None = None
    last_connection_generation: int | None = None
    readiness: FeedReadiness = FeedReadiness.NOT_READY


@dataclass
class SettlementReferenceStore:
    """Current-view store for settlement ticks with append-only ingress retention."""

    ingress: AppendOnlyIngressLog = field(default_factory=AppendOnlyIngressLog)
    _by_symbol: dict[str, SettlementState] = field(default_factory=dict)

    def attach(self, dispatcher: EventDispatcher) -> None:
        dispatcher.subscribe(SettlementReferenceUpdated, self.on_settlement)

    def get(self, symbol: str = "btc/usd") -> SettlementState:
        return self._by_symbol.get(symbol.lower(), SettlementState())

    def mark_not_ready(self, symbol: str = "btc/usd") -> None:
        state = self._by_symbol.get(symbol.lower())
        if state is None:
            self._by_symbol[symbol.lower()] = SettlementState(
                readiness=FeedReadiness.NOT_READY
            )
        else:
            state.readiness = FeedReadiness.NOT_READY
            state.initialized = False

    def on_settlement(self, event: SettlementReferenceUpdated) -> None:
        symbol = event.settlement.symbol.lower()
        state = self._by_symbol.get(symbol, SettlementState())
        accepted = True
        reject_reason = None
        if state.last_ts_event is not None and event.ts_event < state.last_ts_event:
            accepted = False
            reject_reason = "stale_source_ts"
        meta = event.ingress
        if meta is not None:
            self.ingress.append(
                IngressRecord(
                    source=event.source.value,
                    symbol=symbol,
                    role=FeedRole.SETTLEMENT_REFERENCE,
                    source_ts=event.ts_event,
                    receive_wall_utc=event.ts_received,
                    value=str(event.settlement.price),
                    meta=meta,
                    accepted_by_current_view=accepted,
                    reject_reason=reject_reason,
                )
            )
        if not accepted:
            return
        gen = meta.connection_generation if meta else None
        self._by_symbol[symbol] = SettlementState(
            snapshot=event.settlement,
            initialized=True,
            last_ts_event=event.ts_event,
            last_ts_received=event.ts_received,
            last_connection_generation=gen,
            readiness=FeedReadiness.READY,
        )
