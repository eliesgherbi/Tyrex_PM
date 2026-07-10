"""Pure Z-Gap entry gate evaluation — observe-only, no OMS (A0.5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from tyrex_pm.market_data.book_read import QUALITY_MISSING, QUALITY_STALE, PairBookSnapshot
from tyrex_pm.quant.binary_fair_value import FairValueSnapshot, MODEL_STATUS_READY
from tyrex_pm.quant.edge import EDGE_STATUS_READY, EdgeSnapshot
from tyrex_pm.quant.fees import FeeModel
from tyrex_pm.quant.volatility import VolatilitySnapshot
from tyrex_pm.runtime.config import ZGapEntryConfig
from tyrex_pm.runtime.config import Z_GAP_ENTRY_MODE_ENFORCE, Z_GAP_ENTRY_MODE_OBSERVE_ONLY
from tyrex_pm.runtime.time_authority import DEFAULT_ENFORCE_UNCERTAINTY_MAX_MS, SYNC_STATUS_SYNCED, TimeAuthority
from tyrex_pm.state.signal_state_store import (
    BASIS_FRESH,
    BASIS_UNTRUSTED,
    FRESHNESS_FRESH,
    FRESHNESS_LATE,
    FRESHNESS_MISSING,
    FRESHNESS_OBSERVED,
    FRESHNESS_OBSERVED_FROM_LOG,
    FRESHNESS_PENDING,
    FRESHNESS_STALE,
    FRESHNESS_UNTRUSTED,
    SignalSnapshot,
)

DECISION_WOULD_ENTER = "would_enter"
DECISION_SKIP = "skip"
DECISION_NOT_READY = "not_ready"

# Skip / reject reason codes (distinct basis codes required).
REASON_PTB_MISSING = "z_gap_ptb_missing"
REASON_PTB_UNVERIFIED = "z_gap_ptb_unverified"
REASON_PTB_LATE = "z_gap_ptb_late"
REASON_PTB_MISMATCH = "z_gap_ptb_mismatch"
REASON_CLOCK_DRIFT = "z_gap_clock_drift_exceeded"
REASON_CLOCK_SYNC_FAILED = "z_gap_clock_sync_failed"
REASON_FEED_STALE = "z_gap_feed_stale"
REASON_BASIS_EXCEEDED = "z_gap_basis_exceeded_fresh_chainlink"
REASON_CHAINLINK_STALE = "z_gap_chainlink_stale_basis_untrusted"
REASON_Z_OUT_OF_BAND = "z_gap_z_out_of_band"
REASON_TAU_OUT_OF_BAND = "z_gap_tau_out_of_band"
REASON_EDGE_BELOW_THETA = "z_gap_edge_below_theta"
REASON_SIGMA_NOT_READY = "z_gap_sigma_not_ready"
REASON_JUMP_GUARD = "z_gap_jump_guard"
REASON_BOOK_STALE = "z_gap_book_stale"
REASON_QUALITY_REJECT = "z_gap_quality_reject"
REASON_FEE_MODEL_UNKNOWN = "z_gap_fee_model_unknown"


@dataclass(frozen=True)
class ZGapEntryEvaluation:
    decision_status: str
    selected_leg: str | None
    selected_edge: Decimal | None
    reason_code: str | None
    gate_results: dict[str, str]
    fair_value_snapshot: FairValueSnapshot | None
    edge_snapshot: EdgeSnapshot | None
    signal_snapshot: SignalSnapshot | None
    book_snapshot_summary: dict[str, Any] | None
    decision_ts: datetime


def _gate_pass(name: str, results: dict[str, str]) -> None:
    results[name] = "pass"


def _gate_fail(name: str, results: dict[str, str]) -> None:
    results[name] = "fail"


def _gate_na(name: str, results: dict[str, str]) -> None:
    results[name] = "na"


def _abs_basis_bps(signal: SignalSnapshot) -> Decimal | None:
    if signal.basis_bps is None:
        return None
    return abs(signal.basis_bps)


def evaluate_z_gap_entry(
    *,
    signal: SignalSnapshot,
    fair: FairValueSnapshot,
    edge: EdgeSnapshot,
    vol: VolatilitySnapshot,
    books: PairBookSnapshot,
    entry_cfg: ZGapEntryConfig,
    fee_model: FeeModel | None,
    clock_drift_ms: float | None = None,
    time_authority: TimeAuthority | None = None,
    entry_mode: str = Z_GAP_ENTRY_MODE_OBSERVE_ONLY,
    ptb_reference_k: Decimal | None = None,
    quality_reject: bool = False,
    decision_ts: datetime | None = None,
) -> ZGapEntryEvaluation:
    """Evaluate entry gates without submitting orders."""
    ts = decision_ts or signal.snapshot_ts
    gates: dict[str, str] = {}

    def _finish(
        *,
        decision_status: str,
        reason_code: str | None,
        leg: str | None = None,
        edge_val: Decimal | None = None,
    ) -> ZGapEntryEvaluation:
        return ZGapEntryEvaluation(
            decision_status=decision_status,
            selected_leg=leg,
            selected_edge=edge_val,
            reason_code=reason_code,
            gate_results=dict(gates),
            fair_value_snapshot=fair,
            edge_snapshot=edge,
            signal_snapshot=signal,
            book_snapshot_summary=books.to_summary(),
            decision_ts=ts,
        )

    # Clock / time authority — enforce blocks on sync health; observe warns only (no skip on OS drift).
    if entry_mode == Z_GAP_ENTRY_MODE_ENFORCE:
        sync_ok = (
            time_authority is not None
            and time_authority.sync_status == SYNC_STATUS_SYNCED
            and time_authority.uncertainty_ms is not None
            and time_authority.uncertainty_ms <= DEFAULT_ENFORCE_UNCERTAINTY_MAX_MS
        )
        if not sync_ok:
            _gate_fail("clock_sync", gates)
            return _finish(decision_status=DECISION_SKIP, reason_code=REASON_CLOCK_SYNC_FAILED)
        _gate_pass("clock_sync", gates)
    else:
        _gate_pass("clock_sync", gates)
    _gate_na("clock_drift", gates)

    # Feed freshness — Binance
    if signal.binance_freshness in {FRESHNESS_MISSING, FRESHNESS_STALE}:
        _gate_fail("feed_fresh", gates)
        return _finish(decision_status=DECISION_SKIP, reason_code=REASON_FEED_STALE)
    _gate_pass("feed_fresh", gates)

    # Chainlink stale — distinct from basis exceeded
    if signal.chainlink_freshness in {FRESHNESS_MISSING, FRESHNESS_STALE}:
        _gate_fail("chainlink_fresh", gates)
        return _finish(decision_status=DECISION_SKIP, reason_code=REASON_CHAINLINK_STALE)
    _gate_pass("chainlink_fresh", gates)

    # Basis gate — only when Chainlink fresh (distinct from stale-chainlink code above).
    abs_basis = _abs_basis_bps(signal)
    if signal.basis_status == BASIS_UNTRUSTED:
        _gate_fail("basis", gates)
        return _finish(decision_status=DECISION_SKIP, reason_code=REASON_CHAINLINK_STALE)
    if signal.basis_status == BASIS_FRESH and abs_basis is not None:
        if abs_basis > entry_cfg.basis_max_bps:
            _gate_fail("basis", gates)
            return _finish(decision_status=DECISION_SKIP, reason_code=REASON_BASIS_EXCEEDED)
        _gate_pass("basis", gates)
    else:
        _gate_fail("basis", gates)
        return _finish(decision_status=DECISION_NOT_READY, reason_code=REASON_CHAINLINK_STALE)

    # PTB gates
    ptb_status = signal.ptb_status
    if signal.price_to_beat is None or ptb_status in {FRESHNESS_MISSING, FRESHNESS_PENDING}:
        _gate_fail("ptb", gates)
        return _finish(decision_status=DECISION_SKIP, reason_code=REASON_PTB_MISSING)
    if ptb_status == FRESHNESS_LATE:
        _gate_fail("ptb", gates)
        return _finish(decision_status=DECISION_SKIP, reason_code=REASON_PTB_LATE)
    if ptb_status not in {FRESHNESS_OBSERVED, FRESHNESS_OBSERVED_FROM_LOG}:
        _gate_fail("ptb", gates)
        return _finish(decision_status=DECISION_SKIP, reason_code=REASON_PTB_UNVERIFIED)
    if ptb_reference_k is not None and signal.price_to_beat is not None:
        if ptb_reference_k > 0:
            rel_err = abs(signal.price_to_beat - ptb_reference_k) / ptb_reference_k
            if rel_err > Decimal("0.00005"):
                _gate_fail("ptb", gates)
                return _finish(decision_status=DECISION_SKIP, reason_code=REASON_PTB_MISMATCH)
    _gate_pass("ptb", gates)

    # Sigma / jump guard
    if vol.jump_guard_tripped:
        _gate_fail("jump_guard", gates)
        return _finish(decision_status=DECISION_SKIP, reason_code=REASON_JUMP_GUARD)
    _gate_pass("jump_guard", gates)

    if not vol.ready:
        _gate_fail("sigma_ready", gates)
        return _finish(decision_status=DECISION_SKIP, reason_code=REASON_SIGMA_NOT_READY)
    _gate_pass("sigma_ready", gates)

    # Book quality
    if books.up.stale or books.down.stale:
        _gate_fail("book_fresh", gates)
        return _finish(decision_status=DECISION_SKIP, reason_code=REASON_BOOK_STALE)
    if books.up.quality_status in {QUALITY_MISSING, QUALITY_STALE} or books.down.quality_status in {
        QUALITY_MISSING,
        QUALITY_STALE,
    }:
        _gate_fail("book_fresh", gates)
        return _finish(decision_status=DECISION_SKIP, reason_code=REASON_BOOK_STALE)
    if quality_reject:
        _gate_fail("book_quality", gates)
        return _finish(decision_status=DECISION_SKIP, reason_code=REASON_QUALITY_REJECT)
    _gate_pass("book_fresh", gates)
    _gate_pass("book_quality", gates)

    # Fee model
    if fee_model is None or not fee_model.is_resolved:
        _gate_fail("fee_model", gates)
        return _finish(decision_status=DECISION_SKIP, reason_code=REASON_FEE_MODEL_UNKNOWN)
    _gate_pass("fee_model", gates)

    # Model / edge readiness
    if fair.model_status != MODEL_STATUS_READY:
        _gate_fail("fair_value", gates)
        return _finish(
            decision_status=DECISION_NOT_READY,
            reason_code=fair.reject_reason or REASON_SIGMA_NOT_READY,
        )
    _gate_pass("fair_value", gates)

    if edge.edge_status != EDGE_STATUS_READY:
        _gate_fail("edge", gates)
        code = edge.reject_reason or REASON_SIGMA_NOT_READY
        if code == REASON_FEE_MODEL_UNKNOWN:
            return _finish(decision_status=DECISION_SKIP, reason_code=REASON_FEE_MODEL_UNKNOWN)
        return _finish(decision_status=DECISION_NOT_READY, reason_code=code)
    _gate_pass("edge", gates)

    # Tau band
    if fair.tau_s is None:
        _gate_fail("tau_band", gates)
        return _finish(decision_status=DECISION_NOT_READY, reason_code=REASON_TAU_OUT_OF_BAND)
    if fair.tau_s < entry_cfg.tau_band_lo_s or fair.tau_s > entry_cfg.tau_band_hi_s:
        _gate_fail("tau_band", gates)
        return _finish(decision_status=DECISION_SKIP, reason_code=REASON_TAU_OUT_OF_BAND)
    _gate_pass("tau_band", gates)

    # Z band
    if fair.z is None:
        _gate_fail("z_band", gates)
        return _finish(decision_status=DECISION_NOT_READY, reason_code=REASON_Z_OUT_OF_BAND)
    z_abs = Decimal(str(abs(fair.z)))
    if z_abs < entry_cfg.z_band_lo or z_abs > entry_cfg.z_band_hi:
        _gate_fail("z_band", gates)
        return _finish(decision_status=DECISION_SKIP, reason_code=REASON_Z_OUT_OF_BAND)
    _gate_pass("z_band", gates)

    # Position gates — N/A until A0.6/A0.7
    _gate_na("one_position_per_window", gates)
    _gate_na("no_reentry_after_exit", gates)

    # Edge threshold
    assert edge.selected_edge is not None
    selected_leg = edge.selected_leg
    selected_edge = edge.selected_edge
    if selected_edge < entry_cfg.theta_take:
        _gate_fail("theta_take", gates)
        return _finish(
            decision_status=DECISION_SKIP,
            reason_code=REASON_EDGE_BELOW_THETA,
            leg=selected_leg,
            edge_val=selected_edge,
        )
    _gate_pass("theta_take", gates)

    return _finish(
        decision_status=DECISION_WOULD_ENTER,
        reason_code=None,
        leg=selected_leg,
        edge_val=selected_edge,
    )
