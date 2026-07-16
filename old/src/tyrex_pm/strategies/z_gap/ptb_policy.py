"""Deterministic PTB source precedence, locking, and mismatch policy (D2)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from tyrex_pm.ingestion.price_to_beat_tracker import (
    PTB_STATUS_LATE,
    PTB_STATUS_MISSING,
    PTB_STATUS_OBSERVED,
    PTB_STATUS_OBSERVED_FROM_LOG,
    PtbDerivation,
)
from tyrex_pm.state.z_gap_ptb_store import ZGapPtbStore, ZGapPtbWindowRecord

PTB_SOURCE_LIVE = "live_boundary"
PTB_SOURCE_LOG = "log_boundary"
PTB_SOURCE_NONE = "none"

PTB_MISMATCH_TOLERANCE_BPS = Decimal("0.5")
USABLE_STATUSES = frozenset({PTB_STATUS_OBSERVED, PTB_STATUS_OBSERVED_FROM_LOG})
MAX_USABLE_LAG_MS = 5000.0


@dataclass(frozen=True)
class PtbSourceCandidate:
    source: str
    price: str
    status: str
    boundary_lag_ms: float | None

    @property
    def usable(self) -> bool:
        return (
            self.status in USABLE_STATUSES
            and bool(self.price)
            and self.boundary_lag_ms is not None
            and self.boundary_lag_ms <= MAX_USABLE_LAG_MS
        )


@dataclass(frozen=True)
class PtbSelectionResult:
    market_id: str
    event_start_ts: float
    event_end_ts: float
    selected_source: str
    selected_k: str | None
    live_k: str | None
    log_k: str | None
    difference_bps: float | None
    live_boundary_lag_ms: float | None
    log_boundary_lag_ms: float | None
    usable: bool
    locked: bool
    block_reason: str | None
    mismatch: bool

    def to_fact_payload(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "event_start_ts": self.event_start_ts,
            "event_end_ts": self.event_end_ts,
            "selected_source": self.selected_source,
            "selected_k": self.selected_k,
            "live_k": self.live_k,
            "log_k": self.log_k,
            "difference_bps": self.difference_bps,
            "live_boundary_lag_ms": self.live_boundary_lag_ms,
            "log_boundary_lag_ms": self.log_boundary_lag_ms,
            "usable": self.usable,
            "locked": self.locked,
            "block_reason": self.block_reason,
            "mismatch": self.mismatch,
        }


def _candidate_from_live(
    *,
    price: str | None,
    status: str,
    lag_ms: float | None,
) -> PtbSourceCandidate | None:
    if not price:
        return None
    return PtbSourceCandidate(
        source=PTB_SOURCE_LIVE,
        price=str(price),
        status=status,
        boundary_lag_ms=lag_ms,
    )


def _candidate_from_log(derived: PtbDerivation | None) -> PtbSourceCandidate | None:
    if derived is None:
        return None
    return PtbSourceCandidate(
        source=PTB_SOURCE_LOG,
        price=derived.price,
        status=derived.status,
        boundary_lag_ms=derived.boundary_lag_ms,
    )


def _relative_diff_bps(a: str, b: str) -> float | None:
    try:
        da = Decimal(str(a))
        db = Decimal(str(b))
    except (InvalidOperation, ValueError):
        return None
    if db == 0:
        return None
    return float(abs((da - db) / db) * Decimal("10000"))


def select_ptb_source(
    *,
    market_id: str,
    event_start_ts: float,
    event_end_ts: float,
    live_price: str | None = None,
    live_status: str = PTB_STATUS_MISSING,
    live_lag_ms: float | None = None,
    log_derivation: PtbDerivation | None = None,
    reference_k: str | None = None,
    existing: ZGapPtbWindowRecord | None = None,
    experimental_mode: bool = False,
) -> PtbSelectionResult:
    """Apply precedence: live usable > log usable > no usable K."""
    live = _candidate_from_live(price=live_price, status=live_status, lag_ms=live_lag_ms)
    log = _candidate_from_log(log_derivation)

    live_k = live.price if live and live.usable else None
    log_k = log.price if log and log.usable else None
    live_lag = live.boundary_lag_ms if live and live.usable else None
    log_lag = log.boundary_lag_ms if log and log.usable else None

    selected_source = PTB_SOURCE_NONE
    selected_k: str | None = None
    block_reason: str | None = None
    mismatch = False
    diff_bps: float | None = None

    if live and live.usable:
        selected_source = PTB_SOURCE_LIVE
        selected_k = live_k
    elif log and log.usable:
        selected_source = PTB_SOURCE_LOG
        selected_k = log_k
    elif live and live.status == PTB_STATUS_LATE:
        block_reason = "live_ptb_late_debug_only"
    elif log and log.status == PTB_STATUS_LATE:
        block_reason = "log_ptb_late_debug_only"
    else:
        block_reason = "no_usable_ptb"

    if live_k and log_k:
        diff_bps = _relative_diff_bps(live_k, log_k)
        if diff_bps is not None and Decimal(str(diff_bps)) > PTB_MISMATCH_TOLERANCE_BPS:
            mismatch = True
            block_reason = "live_log_mismatch"
            if not experimental_mode:
                selected_k = None
                selected_source = PTB_SOURCE_NONE

    if reference_k and selected_k and not experimental_mode:
        ref_diff = _relative_diff_bps(selected_k, reference_k)
        if ref_diff is not None and Decimal(str(ref_diff)) > PTB_MISMATCH_TOLERANCE_BPS:
            mismatch = True
            block_reason = "reference_k_mismatch"
            selected_k = None
            selected_source = PTB_SOURCE_NONE

    usable = selected_k is not None and selected_source != PTB_SOURCE_NONE
    if mismatch and not experimental_mode:
        usable = False
    locked = usable

    if existing is not None and existing.locked and existing.selected_k:
        if selected_k and selected_k != existing.selected_k:
            mismatch = True
            block_reason = "locked_k_change_rejected"
            selected_k = existing.selected_k
            selected_source = existing.selected_source or selected_source
            usable = existing.usable
            locked = True
        elif not selected_k:
            selected_k = existing.selected_k
            selected_source = existing.selected_source or selected_source
            usable = existing.usable
            locked = True

    return PtbSelectionResult(
        market_id=market_id,
        event_start_ts=event_start_ts,
        event_end_ts=event_end_ts,
        selected_source=selected_source,
        selected_k=selected_k,
        live_k=live_k,
        log_k=log_k,
        difference_bps=diff_bps,
        live_boundary_lag_ms=live_lag,
        log_boundary_lag_ms=log_lag,
        usable=usable,
        locked=locked,
        block_reason=block_reason,
        mismatch=mismatch,
    )


def persist_ptb_selection(
    result: PtbSelectionResult,
    *,
    store: ZGapPtbStore | None = None,
) -> ZGapPtbWindowRecord:
    st = store or ZGapPtbStore()
    record = ZGapPtbWindowRecord(
        market_id=result.market_id,
        event_start_ts=result.event_start_ts,
        event_end_ts=result.event_end_ts,
        selected_source=result.selected_source,
        selected_k=result.selected_k,
        live_k=result.live_k,
        log_k=result.log_k,
        difference_bps=result.difference_bps,
        live_boundary_lag_ms=result.live_boundary_lag_ms,
        log_boundary_lag_ms=result.log_boundary_lag_ms,
        usable=result.usable,
        locked=result.locked,
        block_reason=result.block_reason,
        mismatch=result.mismatch,
    )
    st.save(record)
    return record
