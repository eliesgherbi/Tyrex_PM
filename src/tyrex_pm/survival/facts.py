"""Shared survival fact payload helpers (Phase 1 M7 early)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tyrex_pm.survival.models import ExecutableExitEvidence, SurvivalExitEvaluation, SurvivorTargetPlan


def build_survival_evidence_payload(
    *,
    selected_target: Decimal | None = None,
    target_mode: str | None = None,
    current_executable_bid: Decimal | None = None,
    touch_bid: Decimal | None = None,
    sweep_vwap: Decimal | None = None,
    depth_fraction: Decimal | None = None,
    seconds_to_close: float | None = None,
    decision_action: str | None = None,
    enforcement_mode: str | None = None,
    snapshot_id: str | None = None,
    planner_evidence_ref: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    correlation_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if selected_target is not None:
        payload["selected_target"] = str(selected_target)
    if target_mode is not None:
        payload["target_mode"] = target_mode
    if current_executable_bid is not None:
        payload["current_executable_bid"] = str(current_executable_bid)
    if touch_bid is not None:
        payload["touch_bid"] = str(touch_bid)
    if sweep_vwap is not None:
        payload["sweep_vwap"] = str(sweep_vwap)
    if depth_fraction is not None:
        payload["depth_fraction"] = str(depth_fraction)
    if seconds_to_close is not None:
        payload["seconds_to_close"] = seconds_to_close
    if decision_action is not None:
        payload["decision_action"] = decision_action
    if enforcement_mode is not None:
        payload["enforcement_mode"] = enforcement_mode
    if snapshot_id is not None:
        payload["snapshot_id"] = snapshot_id
    if planner_evidence_ref is not None:
        payload["planner_evidence_ref"] = planner_evidence_ref
    if extra:
        payload.update(extra)
    if correlation_fields:
        payload.update(correlation_fields)
    return payload


def target_plan_payload(plan: SurvivorTargetPlan) -> dict[str, Any]:
    return build_survival_evidence_payload(
        selected_target=plan.trigger_target,
        target_mode=plan.mode.value,
        extra={
            "classification": plan.classification.value,
            "required_survivor_exit_price": str(plan.required_survivor_exit_price),
            "trigger_target": str(plan.trigger_target),
            "target_total_net": str(plan.target_total_net),
            "total_entry_cost": str(plan.total_entry_cost),
            "loser_exit_proceeds": str(plan.loser_exit_proceeds),
            **plan.evidence,
        },
    )


def exit_evaluation_payload(
    evaluation: SurvivalExitEvaluation,
    *,
    requested_qty: Decimal,
) -> dict[str, Any]:
    ev = evaluation.evidence
    return build_survival_evidence_payload(
        current_executable_bid=ev.executable_bid,
        touch_bid=ev.touch_bid,
        sweep_vwap=ev.sweep_vwap,
        depth_fraction=ev.available_depth_fraction,
        snapshot_id=ev.snapshot_id,
        planner_evidence_ref=ev.planner_evidence_ref,
        extra={
            "verdict": evaluation.verdict,
            "reason": evaluation.reason,
            "requested_qty": str(requested_qty),
            "recommended_qty": str(evaluation.recommended_qty),
            "available_depth": str(ev.available_depth),
            "available_depth_fraction": str(ev.available_depth_fraction),
            "expected_slippage": str(ev.expected_slippage) if ev.expected_slippage is not None else None,
            "book_age_ms": ev.book_age_ms,
            "quality_verdict": ev.quality_verdict,
            "spread": str(ev.spread) if ev.spread is not None else None,
            "worst_price_to_fill": str(ev.worst_price_to_fill) if ev.worst_price_to_fill is not None else None,
        },
    )
