"""Dynamic PTB boundary gates for Z-Gap single-session orchestrator."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.ingestion.price_to_beat_tracker import (
    DEFAULT_CHAINLINK_TICKS_PATH,
    PTB_STATUS_LATE,
    PTB_STATUS_MISSING,
    PTB_STATUS_OBSERVED,
    PTB_STATUS_OBSERVED_FROM_LOG,
    derive_ptb_from_chainlink_log,
)
from tyrex_pm.runtime.z_gap_ptb_attestation import evaluate_ptb_attestation, ptb_error_bps
from tyrex_pm.runtime.z_gap_ptb_commissioning import (
    COMMISSIONING_POLICY_NAME,
    independent_ptb_reference_available,
    load_commissioning_certificate,
    validate_commissioning_certificate,
)
from tyrex_pm.state.z_gap_ptb_store import ZGapPtbStore
from tyrex_pm.strategies.z_gap.ptb_policy import persist_ptb_selection, select_ptb_source

PTB_STATUS_WAITING = "WAITING_FOR_BOUNDARY"
PTB_STATUS_CAPTURED = "PTB_CAPTURED"
PTB_STATUS_LOCKED = "PTB_LOCKED"
PTB_STATUS_MISMATCH = "PTB_MISMATCH"
PTB_STATUS_LATE_GATE = "PTB_LATE"
PTB_STATUS_MISSING_GATE = "PTB_MISSING"
PTB_STATUS_INVALID = "PTB_INVALID_NO_TRADE"
PTB_STATUS_READY = "PTB_LOCKED_READY_TO_EVALUATE"


@dataclass(frozen=True)
class BoundaryGateResult:
    status: str
    ready_to_evaluate: bool
    selection: Any
    attestation: dict[str, Any] | None
    block_reason: str | None = None

    @property
    def no_trade(self) -> bool:
        return not self.ready_to_evaluate


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def evaluate_boundary_ptb_gate(
    *,
    market_id: str,
    event_start_ts: float,
    event_end_ts: float,
    now_ts: float,
    live_price: str | None = None,
    live_status: str = PTB_STATUS_MISSING,
    live_lag_ms: float | None = None,
    chainlink_log_path: Path | None = None,
    ptb_config_hash: str = "",
    artifacts_dir: Path | None = None,
    ptb_store_path: Path | None = None,
    experimental_mode: bool = False,
) -> BoundaryGateResult:
    """Capture, compare, lock K at boundary; write dynamic attestation artifact."""
    log_path = chainlink_log_path or DEFAULT_CHAINLINK_TICKS_PATH
    log_derivation = derive_ptb_from_chainlink_log(
        event_start_ts=event_start_ts,
        path=log_path,
    )

    ptb_store = ZGapPtbStore(path=ptb_store_path)
    existing = ptb_store.load(market_id)
    selection = select_ptb_source(
        market_id=market_id,
        event_start_ts=event_start_ts,
        event_end_ts=event_end_ts,
        live_price=live_price,
        live_status=live_status,
        live_lag_ms=live_lag_ms,
        log_derivation=log_derivation,
        reference_k=None,
        existing=existing,
        experimental_mode=experimental_mode,
    )

    status = PTB_STATUS_WAITING
    block_reason = selection.block_reason
    attestation: dict[str, Any] | None = None
    ready = False

    if selection.mismatch:
        status = PTB_STATUS_MISMATCH
    elif selection.selected_k is None:
        if (live_status == PTB_STATUS_LATE) or (log_derivation and log_derivation.status == PTB_STATUS_LATE):
            status = PTB_STATUS_LATE_GATE
            block_reason = block_reason or "boundary_lag_exceeded"
        else:
            status = PTB_STATUS_MISSING_GATE
            block_reason = block_reason or "no_usable_ptb"
    elif selection.usable and selection.selected_k:
        status = PTB_STATUS_CAPTURED
        try:
            persist_ptb_selection(selection, store=ptb_store)
            status = PTB_STATUS_LOCKED
        except ValueError as exc:
            status = PTB_STATUS_INVALID
            block_reason = str(exc)
        else:
            if experimental_mode:
                status = PTB_STATUS_READY if selection.usable else PTB_STATUS_LOCKED
                ready = selection.usable
                attestation = {
                    "generated_at": _utc_now_iso(),
                    "boundary_status": status,
                    "experimental_mode": True,
                    "selected_source": selection.selected_source,
                    "live_k": selection.live_k,
                    "log_k": selection.log_k,
                    "difference_bps": selection.difference_bps,
                    "mismatch": selection.mismatch,
                    "attestation_pass": selection.usable,
                    "enforce_unlock_allowed": selection.usable,
                    "attestation_fail_reason": selection.block_reason if not selection.usable else None,
                    "ptb_error_bps": str(selection.difference_bps) if selection.difference_bps is not None else "0",
                }
            else:
                ref_k: str | None = None
                ref_source = "live_log_agreement"
                err_bps: str | None = "0"
                enforce_unlock = True
                fail_reason: str | None = None

                if independent_ptb_reference_available():
                    ref_source = "polymarket_independent"
                else:
                    cert = load_commissioning_certificate(
                        (artifacts_dir / "ptb_commissioning_certificate.json") if artifacts_dir else None
                    )
                    cert_val = validate_commissioning_certificate(
                        cert,
                        ptb_config_hash=ptb_config_hash,
                        now_ts=now_ts,
                    )
                    if not cert_val.valid:
                        if selection.live_k and selection.log_k:
                            ref_k = selection.log_k if selection.selected_source == "live_boundary" else selection.live_k
                            ref_source = "cross_source_agreement"
                            try:
                                err = ptb_error_bps(
                                    Decimal(selection.selected_k),
                                    Decimal(ref_k),
                                )
                                err_bps = str(err)
                                if err > Decimal("0.5"):
                                    enforce_unlock = False
                                    fail_reason = f"live_log_agreement_error_{err_bps}_bps"
                            except Exception as exc:
                                enforce_unlock = False
                                fail_reason = str(exc)
                        else:
                            enforce_unlock = False
                            fail_reason = cert_val.reason
                    else:
                        ref_k = selection.selected_k
                        ref_source = COMMISSIONING_POLICY_NAME

                report = evaluate_ptb_attestation(
                    K_derived=Decimal(selection.selected_k),
                    K_reference=Decimal(ref_k) if ref_k else Decimal(selection.selected_k),
                    reference_source=ref_source,
                    market_id=market_id,
                    event_start_ts=event_start_ts,
                    data_source="boundary_capture",
                    golden_fixture_only=False,
                    checked_at_utc=_utc_now_iso(),
                )
                attestation = report.to_dict()
                attestation.update(
                    {
                        "generated_at": _utc_now_iso(),
                        "boundary_status": PTB_STATUS_READY,
                        "selected_source": selection.selected_source,
                        "live_k": selection.live_k,
                        "log_k": selection.log_k,
                        "difference_bps": selection.difference_bps,
                        "enforce_unlock_allowed": enforce_unlock and report.attestation_pass,
                        "attestation_pass": enforce_unlock and report.attestation_pass,
                        "attestation_fail_reason": fail_reason or report.attestation_fail_reason,
                        "ptb_error_bps": err_bps or report.ptb_error_bps,
                    }
                )
                if enforce_unlock and report.attestation_pass:
                    status = PTB_STATUS_READY
                    ready = True
                else:
                    status = PTB_STATUS_INVALID
                    block_reason = fail_reason or report.attestation_fail_reason

    if artifacts_dir is not None:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        if attestation is not None:
            (artifacts_dir / "ptb_attestation.json").write_text(
                json.dumps(attestation, indent=2),
                encoding="utf-8",
            )
        lock_payload = selection.to_fact_payload()
        lock_payload["boundary_status"] = status
        lock_payload["evaluated_at"] = _utc_now_iso()
        (artifacts_dir / "ptb_boundary_lock.json").write_text(
            json.dumps(lock_payload, indent=2),
            encoding="utf-8",
        )

    return BoundaryGateResult(
        status=status,
        ready_to_evaluate=ready,
        selection=selection,
        attestation=attestation,
        block_reason=block_reason,
    )
