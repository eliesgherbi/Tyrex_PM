"""Price-to-beat tracker for BTC 5m record mode (M2B.3-A)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from tyrex_pm.core.events import MarketEvent
from tyrex_pm.venue.polymarket_rtds.normalize import build_price_to_beat_observed_event


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _MarketRefState:
    market_id: str
    event_start_ts: float
    event_end_ts: float
    price_to_beat: str | None = None
    price_to_beat_ts: datetime | None = None
    price_to_beat_lag_ms: float | None = None
    raw_reference_event_id: str | None = None
    ptb_status: str = "pending"
    final_reference_price: str | None = None
    final_reference_price_ts: datetime | None = None
    final_reference_lag_ms: float | None = None
    final_status: str = "pending"
    ptb_emitted: bool = False
    final_emitted: bool = False


@dataclass
class PriceToBeatTracker:
    """Derive price-to-beat from Chainlink reference ticks at market boundaries."""

    max_lag_ms: float = 5000.0
    source_label: str = "polymarket_rtds_chainlink"
    _markets: dict[str, _MarketRefState] = field(default_factory=dict)

    def register_market(
        self,
        *,
        market_id: str,
        event_start_ts: float,
        event_end_ts: float,
    ) -> None:
        if market_id in self._markets:
            return
        self._markets[market_id] = _MarketRefState(
            market_id=market_id,
            event_start_ts=event_start_ts,
            event_end_ts=event_end_ts,
        )

    def on_reference_tick(self, event: MarketEvent) -> list[MarketEvent]:
        source_ts = event.source_ts
        if source_ts is None:
            return []
        value = str((event.payload or {}).get("value") or "")
        if not value:
            return []
        out: list[MarketEvent] = []
        source_ms = source_ts.timestamp()
        for state in self._markets.values():
            start_ms = state.event_start_ts
            end_ms = state.event_end_ts
            if state.ptb_status == "pending" and source_ms >= start_ms:
                lag_ms = (source_ms - start_ms) * 1000.0
                if lag_ms <= self.max_lag_ms:
                    state.price_to_beat = value
                    state.price_to_beat_ts = source_ts
                    state.price_to_beat_lag_ms = round(lag_ms, 3)
                    state.raw_reference_event_id = event.event_id
                    state.ptb_status = "observed"
                elif source_ms > start_ms + self.max_lag_ms / 1000.0:
                    state.ptb_status = "missing"
            if state.final_status == "pending" and source_ms >= end_ms:
                lag_ms = (source_ms - end_ms) * 1000.0
                if lag_ms <= self.max_lag_ms:
                    state.final_reference_price = value
                    state.final_reference_price_ts = source_ts
                    state.final_reference_lag_ms = round(lag_ms, 3)
                    state.final_status = "observed"
                elif source_ms > end_ms + self.max_lag_ms / 1000.0:
                    state.final_status = "missing"
            out.extend(self._maybe_emit(state))
        return out

    def flush_missing(self) -> list[MarketEvent]:
        now = _utc_now()
        out: list[MarketEvent] = []
        for state in self._markets.values():
            now_s = now.timestamp()
            if state.ptb_status == "pending" and now_s > state.event_start_ts + self.max_lag_ms / 1000.0:
                state.ptb_status = "missing"
            if state.final_status == "pending" and now_s > state.event_end_ts + self.max_lag_ms / 1000.0:
                state.final_status = "missing"
            out.extend(self._maybe_emit(state))
        return out

    def _maybe_emit(self, state: _MarketRefState) -> list[MarketEvent]:
        out: list[MarketEvent] = []
        recv = _utc_now()
        direction = None
        if state.price_to_beat is not None and state.final_reference_price is not None:
            try:
                ptb = float(state.price_to_beat)
                fin = float(state.final_reference_price)
                if fin > ptb:
                    direction = "up"
                elif fin < ptb:
                    direction = "down"
                else:
                    direction = "flat"
            except (TypeError, ValueError):
                direction = None
        should_emit = False
        status = state.ptb_status
        if state.ptb_status == "missing" and not state.ptb_emitted:
            should_emit = True
        elif state.ptb_status == "observed" and not state.ptb_emitted:
            should_emit = True
        elif state.final_status in {"observed", "missing"} and state.ptb_emitted and not state.final_emitted:
            should_emit = True
            status = state.final_status if state.final_status == "missing" else "complete"
        if not should_emit:
            return out
        if not state.ptb_emitted:
            state.ptb_emitted = True
        else:
            state.final_emitted = True
        out.append(
            build_price_to_beat_observed_event(
                market_id=state.market_id,
                event_start_ts=state.event_start_ts,
                event_end_ts=state.event_end_ts,
                price_to_beat=state.price_to_beat,
                price_to_beat_ts=state.price_to_beat_ts,
                price_to_beat_source=self.source_label,
                price_to_beat_lag_ms=state.price_to_beat_lag_ms,
                raw_reference_event_id=state.raw_reference_event_id,
                status=status,
                recv_ts=recv,
                final_reference_price=state.final_reference_price,
                final_reference_price_ts=state.final_reference_price_ts,
                final_reference_lag_ms=state.final_reference_lag_ms,
                direction_vs_price_to_beat=direction,
            )
        )
        return out
