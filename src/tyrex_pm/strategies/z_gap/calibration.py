"""Minimal machine-readable calibration row from a Z-Gap decision (no notebooks)."""

from __future__ import annotations

from typing import Any

from tyrex_pm.strategies.decisions import StrategyDecision
from tyrex_pm.strategies.z_gap.decision_input import ZGapDecisionSnapshot
from tyrex_pm.strategies.z_gap.valuations import EntryLegValuation


def build_calibration_row(
    *,
    decision_input: ZGapDecisionSnapshot,
    decision: StrategyDecision,
    up_val: EntryLegValuation | None = None,
    down_val: EntryLegValuation | None = None,
    actionable: bool = False,
) -> dict[str, Any]:
    """Structured calibration record — reporting stores it; does not recompute math."""
    model = decision_input.model
    selected = decision.evidence.get("selected_leg")
    row_kind = (
        "actionable"
        if actionable
        else (
            "rejected" if decision.action.value in {"WAIT", "SKIP", "BLOCKED"} else "counterfactual"
        )
    )
    return {
        "schema": "z_gap_calibration_v1",
        "market_id": decision_input.market_id.value,
        "window_id": decision_input.window_id,
        "epoch_id": decision_input.epoch.epoch_id,
        "evaluated_at": decision_input.observed_at.isoformat(),
        "tau_s": model.tau_s,
        "K": None if model.K is None else str(model.K),
        "ptb_quality": None if decision_input.ptb is None else decision_input.ptb.quality.value,
        "S": None if model.S is None else str(model.S),
        "settlement_ref": None if model.S is None else str(model.S),
        "basis_bps": None if model.basis_bps is None else str(model.basis_bps),
        "sigma": model.sigma,
        "z": model.z,
        "p_up": model.p_up,
        "p_down": model.p_down,
        "up_ask": None if decision_input.up_book.ask is None else str(decision_input.up_book.ask),
        "up_bid": None if decision_input.up_book.bid is None else str(decision_input.up_book.bid),
        "down_ask": None
        if decision_input.down_book.ask is None
        else str(decision_input.down_book.ask),
        "down_bid": None
        if decision_input.down_book.bid is None
        else str(decision_input.down_book.bid),
        "fee_rate": str(decision_input.fee_curve.fee_rate),
        "fee_exponent": str(decision_input.fee_curve.exponent),
        "fee_label": "estimated",
        "up_e_settlement": None
        if up_val is None or up_val.e_settlement is None
        else str(up_val.e_settlement),
        "up_e_repricing": None
        if up_val is None or up_val.e_repricing is None
        else str(up_val.e_repricing),
        "down_e_settlement": None
        if down_val is None or down_val.e_settlement is None
        else str(down_val.e_settlement),
        "down_e_repricing": None
        if down_val is None or down_val.e_repricing is None
        else str(down_val.e_repricing),
        "selected_leg": selected,
        "action": decision.action.value,
        "reason_code": decision.reason_code,
        "row_kind": row_kind,
        "trigger": decision_input.trigger,
        "valuation_label": "counterfactual",
    }
