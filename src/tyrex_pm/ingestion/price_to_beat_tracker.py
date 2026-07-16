"""Price-to-beat tracker for BTC 5m record mode (M2B.3-A)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tyrex_pm.core.events import MarketEvent
from tyrex_pm.venue.polymarket_rtds.normalize import build_price_to_beat_observed_event

DEFAULT_CHAINLINK_TICKS_PATH = Path("var/state/chainlink_ticks.jsonl")

PTB_STATUS_PENDING = "pending"
PTB_STATUS_OBSERVED = "observed"
PTB_STATUS_OBSERVED_FROM_LOG = "observed_from_log"
PTB_STATUS_LATE = "late"
PTB_STATUS_MISSING = "missing"
PTB_STATUS_COMPLETE = "complete"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_ts(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


@dataclass(frozen=True)
class PtbDerivation:
    price: str
    source_ts: datetime
    boundary_lag_ms: float
    status: str


@dataclass(frozen=True)
class PtbParseDiagnostics:
    """Counts of skipped sidecar lines while scanning for PTB derivation."""

    skipped_blank_lines: int = 0
    skipped_malformed_json: int = 0
    skipped_invalid_rows: int = 0

    @property
    def had_parse_issues(self) -> bool:
        return (
            self.skipped_blank_lines > 0
            or self.skipped_malformed_json > 0
            or self.skipped_invalid_rows > 0
        )


def derive_ptb_from_chainlink_log(
    *,
    event_start_ts: float,
    path: Path | None = None,
    max_lag_ms: float = 5000.0,
) -> PtbDerivation | None:
    """Find first Chainlink tick with source_ts >= event_start_ts in the sidecar log."""
    derivation, _ = derive_ptb_from_chainlink_log_ex(
        event_start_ts=event_start_ts,
        path=path,
        max_lag_ms=max_lag_ms,
    )
    return derivation


def derive_ptb_from_chainlink_log_ex(
    *,
    event_start_ts: float,
    path: Path | None = None,
    max_lag_ms: float = 5000.0,
) -> tuple[PtbDerivation | None, PtbParseDiagnostics]:
    """Find first usable Chainlink tick; return diagnostics for skipped lines."""
    target = path or DEFAULT_CHAINLINK_TICKS_PATH
    diagnostics = PtbParseDiagnostics()
    if not target.is_file():
        return None, diagnostics
    best: PtbDerivation | None = None
    with target.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                diagnostics = PtbParseDiagnostics(
                    skipped_blank_lines=diagnostics.skipped_blank_lines + 1,
                    skipped_malformed_json=diagnostics.skipped_malformed_json,
                    skipped_invalid_rows=diagnostics.skipped_invalid_rows,
                )
                continue
            text = line.strip()
            try:
                row: dict[str, Any] = json.loads(text)
            except json.JSONDecodeError:
                diagnostics = PtbParseDiagnostics(
                    skipped_blank_lines=diagnostics.skipped_blank_lines,
                    skipped_malformed_json=diagnostics.skipped_malformed_json + 1,
                    skipped_invalid_rows=diagnostics.skipped_invalid_rows,
                )
                continue
            source_raw = row.get("source_ts")
            price = str(row.get("price") or "")
            if not source_raw or not price:
                diagnostics = PtbParseDiagnostics(
                    skipped_blank_lines=diagnostics.skipped_blank_lines,
                    skipped_malformed_json=diagnostics.skipped_malformed_json,
                    skipped_invalid_rows=diagnostics.skipped_invalid_rows + 1,
                )
                continue
            source_ts = _parse_iso_ts(str(source_raw))
            if source_ts is None:
                diagnostics = PtbParseDiagnostics(
                    skipped_blank_lines=diagnostics.skipped_blank_lines,
                    skipped_malformed_json=diagnostics.skipped_malformed_json,
                    skipped_invalid_rows=diagnostics.skipped_invalid_rows + 1,
                )
                continue
            source_s = source_ts.timestamp()
            if source_s < event_start_ts:
                continue
            lag_ms = (source_s - event_start_ts) * 1000.0
            status = PTB_STATUS_OBSERVED_FROM_LOG if lag_ms <= max_lag_ms else PTB_STATUS_LATE
            candidate = PtbDerivation(
                price=price,
                source_ts=source_ts,
                boundary_lag_ms=round(lag_ms, 3),
                status=status,
            )
            if best is None or candidate.source_ts < best.source_ts:
                best = candidate
    return best, diagnostics


@dataclass
class _MarketRefState:
    market_id: str
    event_start_ts: float
    event_end_ts: float
    price_to_beat: str | None = None
    price_to_beat_ts: datetime | None = None
    price_to_beat_lag_ms: float | None = None
    raw_reference_event_id: str | None = None
    ptb_status: str = PTB_STATUS_PENDING
    final_reference_price: str | None = None
    final_reference_price_ts: datetime | None = None
    final_reference_lag_ms: float | None = None
    final_status: str = PTB_STATUS_PENDING
    ptb_emitted: bool = False
    final_emitted: bool = False


@dataclass
class PriceToBeatTracker:
    """Derive price-to-beat from Chainlink reference ticks at market boundaries."""

    max_lag_ms: float = 5000.0
    source_label: str = "polymarket_rtds_chainlink"
    chainlink_log_path: Path = field(default_factory=lambda: DEFAULT_CHAINLINK_TICKS_PATH)
    _markets: dict[str, _MarketRefState] = field(default_factory=dict)

    def register_market(
        self,
        *,
        market_id: str,
        event_start_ts: float,
        event_end_ts: float,
        now_ts: float | None = None,
    ) -> PtbDerivation | None:
        if market_id in self._markets:
            return None
        state = _MarketRefState(
            market_id=market_id,
            event_start_ts=event_start_ts,
            event_end_ts=event_end_ts,
        )
        self._markets[market_id] = state
        derived = derive_ptb_from_chainlink_log(
            event_start_ts=event_start_ts,
            path=self.chainlink_log_path,
            max_lag_ms=self.max_lag_ms,
        )
        if derived is not None:
            state.price_to_beat = derived.price
            state.price_to_beat_ts = derived.source_ts
            state.price_to_beat_lag_ms = derived.boundary_lag_ms
            state.ptb_status = derived.status
        elif now_ts is not None and now_ts > event_start_ts + self.max_lag_ms / 1000.0:
            state.ptb_status = PTB_STATUS_MISSING
        return derived

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
            if state.ptb_status == PTB_STATUS_PENDING and source_ms >= start_ms:
                lag_ms = (source_ms - start_ms) * 1000.0
                if lag_ms <= self.max_lag_ms:
                    state.price_to_beat = value
                    state.price_to_beat_ts = source_ts
                    state.price_to_beat_lag_ms = round(lag_ms, 3)
                    state.raw_reference_event_id = event.event_id
                    state.ptb_status = PTB_STATUS_OBSERVED
                else:
                    state.price_to_beat = value
                    state.price_to_beat_ts = source_ts
                    state.price_to_beat_lag_ms = round(lag_ms, 3)
                    state.raw_reference_event_id = event.event_id
                    state.ptb_status = PTB_STATUS_LATE
            if state.final_status == PTB_STATUS_PENDING and source_ms >= end_ms:
                lag_ms = (source_ms - end_ms) * 1000.0
                if lag_ms <= self.max_lag_ms:
                    state.final_reference_price = value
                    state.final_reference_price_ts = source_ts
                    state.final_reference_lag_ms = round(lag_ms, 3)
                    state.final_status = PTB_STATUS_OBSERVED
                elif source_ms > end_ms + self.max_lag_ms / 1000.0:
                    state.final_status = PTB_STATUS_MISSING
            out.extend(self._maybe_emit(state))
        return out

    def flush_missing(self, *, now_ts: float | None = None) -> list[MarketEvent]:
        now = _utc_now() if now_ts is None else datetime.fromtimestamp(now_ts, tz=timezone.utc)
        out: list[MarketEvent] = []
        for state in self._markets.values():
            now_s = now.timestamp()
            if state.ptb_status == PTB_STATUS_PENDING and now_s > state.event_start_ts + self.max_lag_ms / 1000.0:
                state.ptb_status = PTB_STATUS_MISSING
            if state.final_status == PTB_STATUS_PENDING and now_s > state.event_end_ts + self.max_lag_ms / 1000.0:
                state.final_status = PTB_STATUS_MISSING
            out.extend(self._maybe_emit(state))
        return out

    def _effective_ptb_status(self, state: _MarketRefState) -> str:
        status = state.ptb_status
        if status in {PTB_STATUS_OBSERVED, PTB_STATUS_OBSERVED_FROM_LOG} and state.price_to_beat is None:
            return PTB_STATUS_PENDING
        return status

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
        status = self._effective_ptb_status(state)
        if status == PTB_STATUS_MISSING and not state.ptb_emitted:
            should_emit = True
        elif status in {PTB_STATUS_OBSERVED, PTB_STATUS_OBSERVED_FROM_LOG, PTB_STATUS_LATE} and not state.ptb_emitted:
            if state.price_to_beat is not None:
                should_emit = True
        elif state.final_status in {PTB_STATUS_OBSERVED, PTB_STATUS_MISSING} and state.ptb_emitted and not state.final_emitted:
            should_emit = True
            status = state.final_status if state.final_status == PTB_STATUS_MISSING else PTB_STATUS_COMPLETE
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
