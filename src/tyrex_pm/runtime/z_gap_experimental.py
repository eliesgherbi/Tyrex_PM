"""Experimental Z-Gap workflow helpers (observe + tiny live)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from tyrex_pm.ingestion.price_to_beat_tracker import (
    PTB_STATUS_MISSING,
    PTB_STATUS_OBSERVED,
)
from tyrex_pm.runtime.btc_5m_metadata import Btc5mMarketMetadata
from tyrex_pm.runtime.config import AppConfig, ZGapStrategyConfig
from tyrex_pm.runtime.z_gap_session_runtime import SessionRuntimeHandle

EXPERIMENTAL_LIVE_PHRASE = "APPROVE $5 EXPERIMENT"
MAX_EXPERIMENTAL_USD = Decimal("5")


@dataclass(frozen=True)
class LivePtbReading:
    price: str | None
    status: str
    lag_ms: float | None
    source_ts: float | None
    recv_ts: float | None
    chainlink_source_ts: float | None
    chainlink_recv_ts: float | None


def read_live_ptb_from_handle(handle: SessionRuntimeHandle | None) -> LivePtbReading:
    """Read PTB state from running SignalStateStore (not injected defaults)."""
    if handle is None or handle.coord.signal_state is None:
        return LivePtbReading(
            price=None,
            status=PTB_STATUS_MISSING,
            lag_ms=None,
            source_ts=None,
            recv_ts=None,
            chainlink_source_ts=None,
            chainlink_recv_ts=None,
        )
    snap = handle.coord.signal_state.snapshot()
    src_ts = snap.ptb_observed_ts
    if src_ts is not None and hasattr(src_ts, "timestamp"):
        src_ts = src_ts.timestamp()
    recv_ts = snap.chainlink_recv_ts
    if recv_ts is not None and hasattr(recv_ts, "timestamp"):
        recv_ts = recv_ts.timestamp()
    cl_src = snap.chainlink_source_ts
    if cl_src is not None and hasattr(cl_src, "timestamp"):
        cl_src = cl_src.timestamp()
    return LivePtbReading(
        price=str(snap.price_to_beat) if snap.price_to_beat is not None else None,
        status=str(snap.ptb_status or PTB_STATUS_MISSING),
        lag_ms=snap.ptb_lag_ms,
        source_ts=src_ts,
        recv_ts=recv_ts,
        chainlink_source_ts=cl_src,
        chainlink_recv_ts=recv_ts,
    )


def apply_boundary_k_to_store(handle: SessionRuntimeHandle | None, boundary: Any) -> None:
    """Push locked boundary K into live SignalStateStore when feeds did not set it."""
    if handle is None or handle.coord.signal_state is None:
        return
    sel = getattr(boundary, "selection", None)
    if sel is None or not sel.selected_k or not sel.usable:
        return
    snap = handle.coord.signal_state.snapshot()
    if snap.price_to_beat is not None:
        return
    status = "observed" if sel.selected_source == "live_boundary" else "observed_from_log"
    lag = sel.live_boundary_lag_ms if sel.selected_source == "live_boundary" else sel.log_boundary_lag_ms
    handle.coord.signal_state.lock_price_to_beat(
        Decimal(str(sel.selected_k)),
        status=status,
        lag_ms=lag,
    )


def prompt_experimental_live_approval(
    *,
    market_id: str,
    max_usd: str,
    input_fn: Callable[[str], str] | None = None,
    write_fn: Callable[[str], None] | None = None,
) -> bool:
    out = write_fn or print
    inp = input_fn or input
    out(
        "\n".join(
            [
                "",
                "=== Experimental live ($5 cap) ===",
                f"Market: {market_id}",
                f"Maximum: ${max_usd}",
                "One window, one entry attempt, one position, no reentry.",
                "",
                f"Type exactly: {EXPERIMENTAL_LIVE_PHRASE}",
                "",
            ]
        )
    )
    return inp("> ").strip() == EXPERIMENTAL_LIVE_PHRASE


def record_experimental_approval_in_manifest(
    handle: SessionRuntimeHandle,
    *,
    market_id: str,
    max_usd: str,
    approved_at_ts: float,
) -> None:
    manifest_path = handle.runs_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["experimental_live_approval"] = {
        "approved": True,
        "phrase": EXPERIMENTAL_LIVE_PHRASE,
        "market_id": market_id,
        "maximum_usd": max_usd,
        "approved_at": datetime.fromtimestamp(approved_at_ts, tz=timezone.utc).isoformat(),
    }
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def validate_experimental_live_metadata(zg: ZGapStrategyConfig, *, max_usd: Decimal) -> list[str]:
    """Minimal hard checks for experimental-live (not production certification)."""
    errors: list[str] = []
    if max_usd > MAX_EXPERIMENTAL_USD:
        errors.append(f"maximum_usd {max_usd} exceeds experimental cap {MAX_EXPERIMENTAL_USD}")
    if not zg.market_id or "btc_5m" not in zg.market_id:
        errors.append(f"invalid market_id {zg.market_id!r}")
    if not zg.yes_token_id or not zg.no_token_id:
        errors.append("yes/no token ids required")
    if zg.event_start_ts is None or zg.event_end_ts is None:
        errors.append("event timestamps required")
    return errors


def build_observe_session_report(
    *,
    meta: Btc5mMarketMetadata,
    run_name: str,
    prestart_s: float,
    boundary: Any,
    ptb_reading: LivePtbReading,
    handle: SessionRuntimeHandle | None,
    warnings: list[str],
    oms_submissions: int,
    terminal_status: str,
    facts_path: str | None,
    ptb_trace: Any | None = None,
) -> dict[str, Any]:
    """Assemble observe smoke report with nulls for missing fields."""
    sel = getattr(boundary, "selection", None)
    lock_ts = datetime.now(timezone.utc).isoformat() if getattr(sel, "locked", False) else None
    sigma_ready = bool(handle and handle.sigma_warm)
    feeds_ready = bool(handle and handle.feeds_ready)
    sigma_fields: dict[str, Any] = {}
    if handle is not None:
        sigma_fields = handle.sigma_warmup.to_report_payload()
    clock_status = None
    clock_uncertainty_ms = None
    if handle and handle.coord.time_authority is not None:
        ta = handle.coord.time_authority
        clock_status = ta.sync_status
        clock_uncertainty_ms = ta.uncertainty_ms

    report = {
        "run_name": run_name,
        "mode": "observe_only",
        "terminal_status": terminal_status,
        "market_id": meta.market_id,
        "event_slug": meta.event_slug,
        "event_start_ts": meta.event_start_ts,
        "event_end_ts": meta.event_end_ts,
        "prestart_seconds": prestart_s,
        "feeds_started": feeds_ready,
        "clock_status": clock_status,
        "clock_uncertainty_ms": clock_uncertainty_ms,
        "sigma_ready": sigma_ready,
        "sigma_not_ready": not sigma_ready,
        **sigma_fields,
        "k_value": getattr(sel, "selected_k", None) if sel else ptb_reading.price,
        "k_source": getattr(sel, "selected_source", None) if sel else None,
        "k_source_timestamp": ptb_reading.source_ts,
        "k_receive_timestamp": ptb_reading.recv_ts,
        "boundary_lag_ms": getattr(sel, "live_boundary_lag_ms", None) if sel else ptb_reading.lag_ms,
        "sidecar_k": getattr(sel, "log_k", None) if sel else None,
        "live_k": getattr(sel, "live_k", None) if sel else ptb_reading.price,
        "live_sidecar_difference_bps": getattr(sel, "difference_bps", None) if sel else None,
        "ptb_usable": getattr(sel, "usable", False) if sel else False,
        "ptb_status": getattr(boundary, "status", None),
        "ptb_mismatch": getattr(sel, "mismatch", False) if sel else False,
        "ptb_lock_timestamp": lock_ts,
        "model_usable_for_entry": getattr(boundary, "ready_to_evaluate", False),
        "boundary_block_reason": getattr(boundary, "block_reason", None),
        "oms_submissions": oms_submissions,
        "facts_path": facts_path,
        "warnings": warnings,
        "operator_manual_comparison": {
            "pm_ui_ptb_observed": None,
            "time_checked": None,
            "difference_vs_tyrex": None,
            "notes": None,
        },
    }
    if ptb_trace is not None:
        from tyrex_pm.runtime.z_gap_ptb_capture import capture_trace_to_payload

        report.update(capture_trace_to_payload(ptb_trace))
    return report
