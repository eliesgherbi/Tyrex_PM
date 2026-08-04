"""SummaryAccumulators and run_summary.json construction/checkpointing."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from tyrex_pm.reporting.contracts import (
    ReportingEvent,
    ReportingHealth,
    ReportingLane,
    RunLifecycle,
    utc_now,
)


@dataclass
class ClosestCandidate:
    evaluation_id: str | None
    market_id: str | None
    window_id: str | None
    selected_leg: str | None
    executable_net_edge: str | None
    required_edge: str | None
    signed_margin: float
    action: str | None
    primary_reason: str | None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "market_id": self.market_id,
            "window_id": self.window_id,
            "selected_leg": self.selected_leg,
            "executable_net_edge": self.executable_net_edge,
            "required_edge": self.required_edge,
            "signed_margin": self.signed_margin,
            "action": self.action,
            "primary_reason": self.primary_reason,
            "diagnostics": dict(self.diagnostics),
        }


@dataclass
class SummaryAccumulators:
    """Updated on every emit before analytics sampling/drop decisions."""

    evaluations_attempted: int = 0
    evaluations_completed: int = 0
    evaluations_skipped: int = 0
    detailed_persisted: int = 0
    detailed_sampled_dropped: int = 0
    action_counts: Counter[str] = field(default_factory=Counter)
    primary_reason_counts: Counter[str] = field(default_factory=Counter)
    failed_gate_counts: Counter[str] = field(default_factory=Counter)
    gates_not_evaluated_counts: Counter[str] = field(default_factory=Counter)
    data_quality_failures: int = 0
    intents: int = 0
    risk_approved: int = 0
    risk_denied: int = 0
    risk_reasons: Counter[str] = field(default_factory=Counter)
    plans_created: int = 0
    plans_rejected: int = 0
    orders: int = 0
    fills: int = 0
    cancels: int = 0
    positions: int = 0
    holds: int = 0
    exits: int = 0
    flattens: int = 0
    records_created: int = 0
    records_written_critical: int = 0
    records_written_analytics: int = 0
    records_sampled_dropped: int = 0
    indicator_count: int = 0
    signal_count: int = 0
    closest_by_reason: dict[str, list[ClosestCandidate]] = field(default_factory=dict)
    keep_closest_k: int = 5

    def observe_emit(self, event: ReportingEvent) -> None:
        self.records_created += 1
        family = event.event_family
        payload = dict(event.payload)
        if family == "decision":
            self.evaluations_completed += 1
            action = str(payload.get("action") or "")
            if action:
                self.action_counts[action] += 1
            reason = str(payload.get("reason_code") or payload.get("primary_reason") or "")
            if reason:
                self.primary_reason_counts[reason] += 1
            for g in payload.get("failed_gates") or []:
                self.failed_gate_counts[str(g)] += 1
            for g in payload.get("gates_not_evaluated") or []:
                self.gates_not_evaluated_counts[str(g)] += 1
            for gate in payload.get("gates") or []:
                if isinstance(gate, Mapping) and gate.get("status") == "failed":
                    self.failed_gate_counts[str(gate.get("code"))] += 1
                if isinstance(gate, Mapping) and gate.get("status") == "not_evaluated":
                    self.gates_not_evaluated_counts[str(gate.get("code"))] += 1
        elif family == "data_health":
            if payload.get("failure") or payload.get("valid") is False:
                self.data_quality_failures += 1
            if payload.get("skipped"):
                self.evaluations_skipped += 1
            else:
                self.evaluations_attempted += 1
        elif family == "indicator":
            self.indicator_count += 1
        elif family == "signal":
            self.signal_count += 1
        elif family == "intent":
            self.intents += 1
        elif family == "risk":
            if payload.get("approved") is True or str(payload.get("decision")).upper() == "ALLOW":
                self.risk_approved += 1
            else:
                self.risk_denied += 1
            for r in payload.get("reasons") or payload.get("reason_codes") or []:
                self.risk_reasons[str(r)] += 1
        elif family == "plan":
            if payload.get("rejected") or str(payload.get("status")).lower() in {
                "rejected",
                "failed",
            }:
                self.plans_rejected += 1
            else:
                self.plans_created += 1
        elif family == "order":
            self.orders += 1
        elif family == "fill":
            self.fills += 1
        elif family == "cancel":
            self.cancels += 1
        elif family == "position":
            self.positions += 1
        elif family == "lifecycle":
            action = str(payload.get("action") or payload.get("to_state") or "").upper()
            if "HOLD" in action:
                self.holds += 1
        elif family == "exit":
            self.exits += 1
        elif family == "flatten":
            self.flattens += 1

    def note_persisted(self, event: ReportingEvent, *, dropped: bool = False) -> None:
        if dropped:
            self.records_sampled_dropped += 1
            if event.event_family == "decision":
                self.detailed_sampled_dropped += 1
            return
        if event.lane is ReportingLane.CRITICAL:
            self.records_written_critical += 1
        else:
            self.records_written_analytics += 1
            if event.event_family == "decision":
                self.detailed_persisted += 1

    def consider_closest(
        self,
        *,
        primary_reason: str,
        candidate: ClosestCandidate,
    ) -> None:
        bucket = self.closest_by_reason.setdefault(primary_reason, [])
        bucket.append(candidate)
        bucket.sort(key=lambda c: c.signed_margin, reverse=True)
        del bucket[self.keep_closest_k :]

    def closest_section(self) -> dict[str, Any]:
        if not self.closest_by_reason:
            return {
                "rule": "executable_net_edge_margin",
                "candidates_by_reason": {},
                "no_comparable_candidate": True,
            }
        return {
            "rule": "executable_net_edge_margin",
            "candidates_by_reason": {
                reason: [c.to_dict() for c in items]
                for reason, items in sorted(self.closest_by_reason.items())
            },
            "no_comparable_candidate": False,
        }


def build_run_summary(
    *,
    accumulators: SummaryAccumulators,
    identity: Mapping[str, Any],
    status: Mapping[str, Any],
    configuration: Mapping[str, Any],
    performance_label: str,
    reporting_health: ReportingHealth,
    health_transitions: list[dict[str, Any]],
    last_durable_sequence: int,
    checkpoint_time: datetime | None,
    artifacts: Mapping[str, Any],
    simulation_assumptions: Mapping[str, Any] | None = None,
    extra_sections: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "identity": dict(identity),
        "status": dict(status),
        "configuration": dict(configuration),
        "data_health": {
            "data_quality_failures": accumulators.data_quality_failures,
            "evaluations_skipped": accumulators.evaluations_skipped,
        },
        "strategy_evaluations": {
            "attempted": accumulators.evaluations_attempted,
            "completed": accumulators.evaluations_completed,
            "skipped": accumulators.evaluations_skipped,
            "detailed_persisted": accumulators.detailed_persisted,
            "detailed_sampled_dropped": accumulators.detailed_sampled_dropped,
        },
        "decisions_and_gates": {
            "action_counts": dict(accumulators.action_counts),
            "primary_reason_distribution": dict(accumulators.primary_reason_counts),
            "all_failed_gates": dict(accumulators.failed_gate_counts),
            "gates_not_evaluated": dict(accumulators.gates_not_evaluated_counts),
        },
        "closest_candidates": accumulators.closest_section(),
        "intent_and_risk": {
            "intents": accumulators.intents,
            "risk_approved": accumulators.risk_approved,
            "risk_denied": accumulators.risk_denied,
            "risk_reasons": dict(accumulators.risk_reasons),
            "plans_created": accumulators.plans_created,
            "plans_rejected": accumulators.plans_rejected,
        },
        "execution": {
            "orders": accumulators.orders,
            "fills": accumulators.fills,
            "cancels": accumulators.cancels,
        },
        "positions_and_exits": {
            "positions": accumulators.positions,
            "holds": accumulators.holds,
            "exits": accumulators.exits,
            "flattens": accumulators.flattens,
        },
        "performance": {
            "label": performance_label,
            "simulation_assumptions": dict(simulation_assumptions or {}),
        },
        "reporting_health": {
            "state": reporting_health.value,
            "transitions": list(health_transitions),
            "records_created": accumulators.records_created,
            "records_written_critical": accumulators.records_written_critical,
            "records_written_analytics": accumulators.records_written_analytics,
            "records_sampled_dropped": accumulators.records_sampled_dropped,
            "last_durable_sequence": last_durable_sequence,
            "latest_checkpoint": None
            if checkpoint_time is None
            else checkpoint_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
        "artifacts": dict(artifacts),
    }
    if extra_sections:
        for key, value in extra_sections.items():
            if key not in summary:
                summary[key] = value
    return summary


def classify_stale_running(
    manifest: Mapping[str, Any],
    *,
    now: datetime | None = None,
    stale_after_s: float = 300.0,
) -> dict[str, Any]:
    """Classify a checkpoint left in RUNNING after hard crash.

    Rule (M2): if lifecycle is RUNNING and (now - last_checkpoint_at) exceeds
    ``stale_after_s`` (default 300s), classify as ABORTED/incomplete.
    The dead process cannot update status; this is inspection-time only.
    """
    now = now or utc_now()
    lifecycle = str(manifest.get("lifecycle") or "")
    if lifecycle != RunLifecycle.RUNNING.value:
        return {
            "stale": False,
            "classified_as": lifecycle or None,
            "reason": "not_running",
        }
    raw_ts = manifest.get("last_checkpoint_at") or manifest.get("updated_at")
    if not raw_ts:
        return {
            "stale": True,
            "classified_as": RunLifecycle.ABORTED.value,
            "reason": "running_without_checkpoint_timestamp",
        }
    try:
        ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
    except ValueError:
        return {
            "stale": True,
            "classified_as": RunLifecycle.ABORTED.value,
            "reason": "invalid_checkpoint_timestamp",
        }
    age = (now - ts.astimezone(timezone.utc)).total_seconds()
    if age > stale_after_s:
        return {
            "stale": True,
            "classified_as": RunLifecycle.ABORTED.value,
            "reason": "stale_running_exceeded_threshold",
            "age_s": age,
            "stale_after_s": stale_after_s,
        }
    return {
        "stale": False,
        "classified_as": RunLifecycle.RUNNING.value,
        "reason": "within_stale_threshold",
        "age_s": age,
    }
