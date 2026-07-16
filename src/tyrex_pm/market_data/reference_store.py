"""Authoritative Binance (external) reference price state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tyrex_pm.core.events import ReferencePriceUpdated
from tyrex_pm.core.snapshots import ReferencePriceSnapshot
from tyrex_pm.engine.dispatcher import EventDispatcher


@dataclass
class ReferenceState:
    snapshot: ReferencePriceSnapshot | None = None
    initialized: bool = False
    last_ts_event: datetime | None = None
    last_ts_received: datetime | None = None


class ReferenceDataStore:
    def __init__(self) -> None:
        self._by_symbol: dict[str, ReferenceState] = {}

    def attach(self, dispatcher: EventDispatcher) -> None:
        dispatcher.subscribe(ReferencePriceUpdated, self.on_reference)

    def get(self, symbol: str) -> ReferenceState:
        return self._by_symbol.get(symbol.upper(), ReferenceState())

    def on_reference(self, event: ReferencePriceUpdated) -> None:
        symbol = event.reference.symbol.upper()
        self._by_symbol[symbol] = ReferenceState(
            snapshot=event.reference,
            initialized=True,
            last_ts_event=event.ts_event,
            last_ts_received=event.ts_received,
        )
