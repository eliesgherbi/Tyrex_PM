"""Bounded PTB capture at event boundary for experimental Z-Gap workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from tyrex_pm.ingestion.price_to_beat_tracker import (
    DEFAULT_CHAINLINK_TICKS_PATH,
    PTB_STATUS_OBSERVED,
    PTB_STATUS_OBSERVED_FROM_LOG,
    derive_ptb_from_chainlink_log,
)
from tyrex_pm.runtime.btc_5m_metadata import Btc5mMarketMetadata
from tyrex_pm.runtime.config import AppConfig, ZGapPtbConfig
from tyrex_pm.runtime.signal_feed_runtime import apply_market_event_to_signal_store
from tyrex_pm.runtime.z_gap_experimental import LivePtbReading, read_live_ptb_from_handle
from tyrex_pm.runtime.z_gap_session_runtime import SessionRuntimeHandle

PTB_CAPTURE_WAITING = "WAITING_FOR_PTB"


@dataclass(frozen=True)
class PtbCaptureTrace:
    event_start_ts: float
    ptb_capture_started_at: float
    ptb_capture_deadline: float
    ptb_capture_ended_at: float
    wait_duration_ms: float
    poll_count: int
    live_candidate_k: str | None
    live_source_ts: float | None
    live_receive_ts: float | None
    live_boundary_lag_ms: float | None
    sidecar_candidate_k: str | None
    sidecar_source_ts: float | None
    sidecar_boundary_lag_ms: float | None
    selected_k: str | None
    selected_source: str | None
    selected_boundary_lag_ms: float | None
    ptb_status: str
    ptb_failure_reason: str | None
    capture_outcome: str


def _ts_value(raw: float | datetime | None) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.timestamp()
    return float(raw)


def _sidecar_candidates(
    *,
    event_start_ts: float,
    max_lag_ms: float,
    chainlink_log_path: Any = None,
) -> tuple[str | None, float | None, float | None]:
    derived = derive_ptb_from_chainlink_log(
        event_start_ts=event_start_ts,
        path=chainlink_log_path or DEFAULT_CHAINLINK_TICKS_PATH,
        max_lag_ms=max_lag_ms,
    )
    if derived is None:
        return None, None, None
    return derived.price, derived.source_ts.timestamp(), derived.boundary_lag_ms


def _selected_boundary_lag(selection: Any) -> float | None:
    if selection is None:
        return None
    if selection.selected_source == "live_boundary":
        return selection.live_boundary_lag_ms
    if selection.selected_source == "log_boundary":
        return selection.log_boundary_lag_ms
    return selection.live_boundary_lag_ms or selection.log_boundary_lag_ms


def build_ptb_capture_trace(
    *,
    meta: Btc5mMarketMetadata,
    boundary: Any,
    ptb_reading: LivePtbReading,
    capture_started_at: float,
    capture_deadline: float,
    capture_ended_at: float,
    poll_count: int,
    max_lag_ms: float,
    chainlink_log_path: Any = None,
) -> PtbCaptureTrace:
    sel = getattr(boundary, "selection", None)
    sidecar_k, sidecar_src, sidecar_lag = _sidecar_candidates(
        event_start_ts=meta.event_start_ts,
        max_lag_ms=max_lag_ms,
        chainlink_log_path=chainlink_log_path,
    )
    selected_k = getattr(sel, "selected_k", None) if sel else ptb_reading.price
    selected_source = getattr(sel, "selected_source", None) if sel else None
    status = str(getattr(boundary, "status", "PTB_MISSING"))
    usable = bool(getattr(sel, "usable", False)) if sel else False
    if usable and selected_k:
        outcome = "PTB_LOCKED"
    elif status in {"PTB_LATE", "PTB_LATE_GATE"}:
        outcome = "PTB_LATE"
    elif status in {"PTB_INVALID", "PTB_INVALID_NO_TRADE"}:
        outcome = "PTB_INVALID"
    else:
        outcome = "PTB_MISSING"
    return PtbCaptureTrace(
        event_start_ts=meta.event_start_ts,
        ptb_capture_started_at=capture_started_at,
        ptb_capture_deadline=capture_deadline,
        ptb_capture_ended_at=capture_ended_at,
        wait_duration_ms=round((capture_ended_at - capture_started_at) * 1000.0, 1),
        poll_count=poll_count,
        live_candidate_k=ptb_reading.price or getattr(sel, "live_k", None),
        live_source_ts=ptb_reading.source_ts,
        live_receive_ts=ptb_reading.recv_ts,
        live_boundary_lag_ms=ptb_reading.lag_ms or getattr(sel, "live_boundary_lag_ms", None),
        sidecar_candidate_k=sidecar_k or getattr(sel, "log_k", None),
        sidecar_source_ts=sidecar_src,
        sidecar_boundary_lag_ms=sidecar_lag or getattr(sel, "log_boundary_lag_ms", None),
        selected_k=selected_k,
        selected_source=selected_source,
        selected_boundary_lag_ms=_selected_boundary_lag(sel),
        ptb_status=status,
        ptb_failure_reason=getattr(boundary, "block_reason", None),
        capture_outcome=outcome,
    )


def capture_trace_to_payload(trace: PtbCaptureTrace) -> dict[str, Any]:
    return {
        "event_start_ts": trace.event_start_ts,
        "ptb_capture_started_at": datetime.fromtimestamp(
            trace.ptb_capture_started_at, tz=timezone.utc
        ).isoformat(),
        "ptb_capture_deadline": datetime.fromtimestamp(
            trace.ptb_capture_deadline, tz=timezone.utc
        ).isoformat(),
        "ptb_capture_ended_at": datetime.fromtimestamp(
            trace.ptb_capture_ended_at, tz=timezone.utc
        ).isoformat(),
        "wait_duration_ms": trace.wait_duration_ms,
        "poll_count": trace.poll_count,
        "live_candidate_k": trace.live_candidate_k,
        "live_source_ts": trace.live_source_ts,
        "live_receive_ts": trace.live_receive_ts,
        "live_boundary_lag_ms": trace.live_boundary_lag_ms,
        "sidecar_candidate_k": trace.sidecar_candidate_k,
        "sidecar_source_ts": trace.sidecar_source_ts,
        "sidecar_boundary_lag_ms": trace.sidecar_boundary_lag_ms,
        "selected_k": trace.selected_k,
        "selected_source": trace.selected_source,
        "selected_boundary_lag_ms": trace.selected_boundary_lag_ms,
        "ptb_status": trace.ptb_status,
        "ptb_failure_reason": trace.ptb_failure_reason,
        "capture_outcome": trace.capture_outcome,
    }


def _usable_live_reading(reading: LivePtbReading, *, event_start_ts: float, max_lag_ms: float) -> bool:
    if reading.price is None:
        return False
    if reading.status not in {PTB_STATUS_OBSERVED, PTB_STATUS_OBSERVED_FROM_LOG, "observed", "observed_from_log"}:
        return False
    if reading.source_ts is None or reading.source_ts < event_start_ts:
        return False
    if reading.lag_ms is not None and reading.lag_ms > max_lag_ms:
        return False
    return True


def _flush_tracker_events(handle: SessionRuntimeHandle, *, now_ts: float) -> None:
    tracker = (
        handle.signal_feed_state.price_to_beat_tracker
        if handle.signal_feed_state is not None
        else None
    )
    if tracker is None or handle.coord.signal_state is None:
        return
    for event in tracker.flush_missing(now_ts=now_ts):
        apply_market_event_to_signal_store(handle.coord.signal_state, event)


async def capture_ptb_at_boundary(
    *,
    handle: SessionRuntimeHandle,
    meta: Btc5mMarketMetadata,
    ptb_config: ZGapPtbConfig,
    evaluate_boundary: Callable[..., Awaitable[Any]],
    app: AppConfig,
    experimental_mode: bool,
    now_ts: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    write_fn: Callable[[str], None] | None = None,
) -> tuple[Any, PtbCaptureTrace, LivePtbReading]:
    """Wait until boundary, then poll live runtime + sidecar for first usable K."""
    event_start_ts = meta.event_start_ts
    while now_ts() < event_start_ts:
        await sleep(min(0.25, max(0.01, event_start_ts - now_ts())))

    capture_started_at = now_ts()
    capture_deadline = event_start_ts + ptb_config.capture_wait_timeout_ms / 1000.0
    if write_fn:
        write_fn(f"\n{PTB_CAPTURE_WAITING}\n")

    boundary: Any | None = None
    poll_count = 0
    while True:
        now = now_ts()
        poll_count += 1
        if now >= capture_deadline:
            _flush_tracker_events(handle, now_ts=now)

        boundary = await evaluate_boundary(app, meta, experimental_mode=experimental_mode)
        reading = read_live_ptb_from_handle(handle)
        sel = getattr(boundary, "selection", None)
        captured = bool(sel and sel.usable and sel.selected_k)
        if not captured:
            captured = _usable_live_reading(
                reading,
                event_start_ts=event_start_ts,
                max_lag_ms=ptb_config.max_usable_boundary_lag_ms,
            )
        if captured:
            break
        if now >= capture_deadline:
            break
        await sleep(
            min(
                ptb_config.poll_interval_ms / 1000.0,
                max(0.01, capture_deadline - now),
            )
        )

    capture_ended_at = now_ts()
    reading = read_live_ptb_from_handle(handle)
    trace = build_ptb_capture_trace(
        meta=meta,
        boundary=boundary,
        ptb_reading=reading,
        capture_started_at=capture_started_at,
        capture_deadline=capture_deadline,
        capture_ended_at=capture_ended_at,
        poll_count=poll_count,
        max_lag_ms=ptb_config.max_usable_boundary_lag_ms,
    )
    return boundary, trace, reading
