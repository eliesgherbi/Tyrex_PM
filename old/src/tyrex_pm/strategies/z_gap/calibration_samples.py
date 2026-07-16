"""Calibration sample capture for Z-Gap observe-only windows (A0.5)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tyrex_pm.quant.binary_fair_value import FairValueSnapshot
from tyrex_pm.quant.edge import EdgeSnapshot
from tyrex_pm.quant.fees import FeeModel
from tyrex_pm.quant.volatility import VolatilitySnapshot
from tyrex_pm.state.signal_state_store import (
    FRESHNESS_LATE,
    FRESHNESS_MISSING,
    FRESHNESS_PENDING,
    SignalSnapshot,
)
from tyrex_pm.strategies.z_gap.entry_eval import DECISION_WOULD_ENTER, ZGapEntryEvaluation

DEFAULT_CALIBRATION_SAMPLES_PATH = Path("var/reporting/z_gap/calibration_samples.jsonl")
DEFAULT_INFRA_DEBUG_SAMPLES_PATH = Path("var/reporting/z_gap/observe_infra_debug_samples.jsonl")
FACT_TYPE_CALIBRATION_SAMPLE = "calibration_sample"


def is_calibration_usable_ptb(signal: SignalSnapshot | None) -> bool:
    if signal is None:
        return False
    if signal.price_to_beat is None:
        return False
    if signal.ptb_status in {FRESHNESS_MISSING, FRESHNESS_PENDING, FRESHNESS_LATE}:
        return False
    return True


@dataclass(frozen=True)
class CalibrationSampleRow:
    market_id: str
    condition_id: str | None
    entry_mode: str
    would_have_entered: bool
    selected_leg: str | None
    skip_reason_top: str | None
    p_up_at_decision: str | None
    p_down_at_decision: str | None
    z_at_decision: str | None
    edge_up: str | None
    edge_down: str | None
    basis_bps: str | None
    sigma: str | None
    sigma_units: str | None
    ptb_status: str | None
    ptb_lag_ms: float | None
    fee_model_id: str | None
    resolved_outcome: str | None
    recorded_at_utc: str
    calibration_usable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "condition_id": self.condition_id,
            "entry_mode": self.entry_mode,
            "would_have_entered": self.would_have_entered,
            "selected_leg": self.selected_leg,
            "skip_reason_top": self.skip_reason_top,
            "p_up_at_decision": self.p_up_at_decision,
            "p_down_at_decision": self.p_down_at_decision,
            "z_at_decision": self.z_at_decision,
            "edge_up": self.edge_up,
            "edge_down": self.edge_down,
            "basis_bps": self.basis_bps,
            "sigma": self.sigma,
            "sigma_units": self.sigma_units,
            "ptb_status": self.ptb_status,
            "ptb_lag_ms": self.ptb_lag_ms,
            "fee_model_id": self.fee_model_id,
            "resolved_outcome": self.resolved_outcome,
            "recorded_at_utc": self.recorded_at_utc,
            "calibration_usable": self.calibration_usable,
        }


def build_calibration_sample_row(
    *,
    market_id: str,
    condition_id: str | None,
    entry_mode: str,
    evaln: ZGapEntryEvaluation,
    fair: FairValueSnapshot | None,
    edge: EdgeSnapshot | None,
    vol: VolatilitySnapshot | None,
    signal: SignalSnapshot | None,
    fee_model: FeeModel | None,
    resolved_outcome: str | None = None,
    calibration_usable: bool | None = None,
) -> CalibrationSampleRow:
    usable = calibration_usable if calibration_usable is not None else is_calibration_usable_ptb(signal)
    return CalibrationSampleRow(
        market_id=market_id,
        condition_id=condition_id,
        entry_mode=entry_mode,
        would_have_entered=evaln.decision_status == DECISION_WOULD_ENTER,
        selected_leg=evaln.selected_leg,
        skip_reason_top=evaln.reason_code,
        p_up_at_decision=str(fair.p_up) if fair and fair.p_up is not None else None,
        p_down_at_decision=str(fair.p_down) if fair and fair.p_down is not None else None,
        z_at_decision=str(fair.z) if fair and fair.z is not None else None,
        edge_up=str(edge.edge_up) if edge and edge.edge_up is not None else None,
        edge_down=str(edge.edge_down) if edge and edge.edge_down is not None else None,
        basis_bps=str(signal.basis_bps) if signal and signal.basis_bps is not None else None,
        sigma=str(vol.sigma) if vol and vol.sigma is not None else None,
        sigma_units=vol.sigma_units if vol else None,
        ptb_status=signal.ptb_status if signal else None,
        ptb_lag_ms=signal.ptb_lag_ms if signal else None,
        fee_model_id=fee_model.fee_model_id if fee_model else None,
        resolved_outcome=resolved_outcome,
        recorded_at_utc=datetime.now(timezone.utc).isoformat(),
        calibration_usable=usable,
    )


def append_calibration_sample(
    row: CalibrationSampleRow,
    *,
    path: Path | None = None,
    infra_debug_path: Path | None = None,
) -> Path:
    if row.calibration_usable:
        target = path or DEFAULT_CALIBRATION_SAMPLES_PATH
    else:
        target = infra_debug_path or DEFAULT_INFRA_DEBUG_SAMPLES_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row.to_dict(), sort_keys=True) + "\n")
    return target


def build_calibration_sample_fact_payload(row: CalibrationSampleRow) -> dict[str, Any]:
    return row.to_dict()
