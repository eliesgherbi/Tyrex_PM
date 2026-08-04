"""Adapters: map existing runtime values to ReportingEvent payloads (no recompute)."""

from __future__ import annotations

from typing import Any, Mapping

from tyrex_pm.reporting.contracts import (
    EventFamily,
    LineageIds,
    ReportingLane,
    StrategyDiagnosticsBlob,
)
from tyrex_pm.reporting.reporter import RunReporter


def emit_data_health(
    reporter: RunReporter,
    *,
    payload: Mapping[str, Any],
    producer: str = "observe_host",
    force_critical: bool = False,
    causation_id: str | None = None,
) -> None:
    reporter.emit_dict(
        event_family=EventFamily.DATA_HEALTH.value,
        event_type="data_health.assessment",
        payload=payload,
        producer=producer,
        force_critical=force_critical,
        causation_id=causation_id,
    )


def emit_indicator(
    reporter: RunReporter,
    *,
    payload: Mapping[str, Any],
    producer: str = "observe_host",
    causation_id: str | None = None,
) -> None:
    reporter.emit_dict(
        event_family=EventFamily.INDICATOR.value,
        event_type="indicator.result",
        payload=payload,
        producer=producer,
        causation_id=causation_id,
    )


def emit_signal(
    reporter: RunReporter,
    *,
    payload: Mapping[str, Any],
    producer: str = "observe_host",
    strategy_id: str | None = None,
    causation_id: str | None = None,
) -> None:
    reporter.emit_dict(
        event_family=EventFamily.SIGNAL.value,
        event_type="signal.emitted",
        payload=payload,
        producer=producer,
        strategy_id=None if strategy_id is None else str(strategy_id),
        causation_id=causation_id,
    )


def emit_decision_from_eval(
    reporter: RunReporter,
    *,
    action: str,
    reason_code: str,
    decision_id: str | None,
    evaluation_id: str | None,
    gates: list[dict[str, Any]] | None = None,
    diagnostics: StrategyDiagnosticsBlob | None = None,
    closest_candidate: Mapping[str, Any] | None = None,
    intent_emitted: bool = False,
    blocked_safety: bool = False,
    producer: str = "strategy_binding",
    strategy_id: str | None = None,
    strategy_version: str | None = None,
    market_id: str | None = None,
    window_id: str | None = None,
    causation_id: str | None = None,
    correlation_id: str | None = None,
    extra_payload: Mapping[str, Any] | None = None,
) -> None:
    failed = [
        str(g.get("code"))
        for g in (gates or [])
        if isinstance(g, Mapping) and g.get("status") == "failed"
    ]
    not_eval = [
        str(g.get("code"))
        for g in (gates or [])
        if isinstance(g, Mapping) and g.get("status") == "not_evaluated"
    ]
    payload: dict[str, Any] = {
        "action": action,
        "reason_code": reason_code,
        "primary_reason": reason_code,
        "failed_gates": failed,
        "gates_not_evaluated": not_eval,
        "gates": list(gates or []),
        "intent_emitted": intent_emitted,
    }
    if closest_candidate is not None:
        payload["closest_candidate"] = dict(closest_candidate)
    if extra_payload:
        payload.update(dict(extra_payload))
    force_critical = bool(intent_emitted or blocked_safety)
    reporter.emit_dict(
        event_family=EventFamily.DECISION.value,
        event_type="decision.evaluated",
        payload=payload,
        producer=producer,
        force_critical=force_critical,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        market_id=market_id,
        window_id=window_id,
        causation_id=causation_id,
        correlation_id=correlation_id,
        lineage=LineageIds(evaluation_id=evaluation_id, decision_id=decision_id),
        strategy_diagnostics=diagnostics,
    )


def emit_intent(
    reporter: RunReporter,
    *,
    payload: Mapping[str, Any],
    lineage: LineageIds | None = None,
    producer: str = "observe_host",
    causation_id: str | None = None,
) -> None:
    reporter.emit_dict(
        event_family=EventFamily.INTENT.value,
        event_type="intent.created",
        payload=payload,
        producer=producer,
        lane=ReportingLane.CRITICAL,
        lineage=lineage,
        causation_id=causation_id,
    )


def emit_risk(
    reporter: RunReporter,
    *,
    payload: Mapping[str, Any],
    lineage: LineageIds | None = None,
    producer: str = "risk_engine",
    causation_id: str | None = None,
) -> None:
    reporter.emit_dict(
        event_family=EventFamily.RISK.value,
        event_type="risk.decided",
        payload=payload,
        producer=producer,
        lane=ReportingLane.CRITICAL,
        lineage=lineage,
        causation_id=causation_id,
    )


def emit_plan(
    reporter: RunReporter,
    *,
    payload: Mapping[str, Any],
    lineage: LineageIds | None = None,
    producer: str = "planner",
    causation_id: str | None = None,
) -> None:
    reporter.emit_dict(
        event_family=EventFamily.PLAN.value,
        event_type="plan.created",
        payload=payload,
        producer=producer,
        lane=ReportingLane.CRITICAL,
        lineage=lineage,
        causation_id=causation_id,
    )


def emit_lifecycle(
    reporter: RunReporter,
    *,
    payload: Mapping[str, Any],
    producer: str = "shadow_host",
    causation_id: str | None = None,
) -> None:
    reporter.emit_dict(
        event_family=EventFamily.LIFECYCLE.value,
        event_type="lifecycle.transition",
        payload=payload,
        producer=producer,
        lane=ReportingLane.CRITICAL,
        causation_id=causation_id,
    )


def emit_operational(
    reporter: RunReporter,
    *,
    event_type: str,
    payload: Mapping[str, Any],
    producer: str = "n7_operator",
) -> None:
    reporter.emit_dict(
        event_family=EventFamily.OPERATIONAL.value,
        event_type=event_type,
        payload=payload,
        producer=producer,
        lane=ReportingLane.CRITICAL,
        severity="info",
    )


def emit_order_fill(
    reporter: RunReporter,
    *,
    family: str,
    event_type: str,
    payload: Mapping[str, Any],
    lineage: LineageIds | None = None,
    producer: str = "oms",
) -> None:
    reporter.emit_dict(
        event_family=family,
        event_type=event_type,
        payload=payload,
        producer=producer,
        lane=ReportingLane.CRITICAL,
        lineage=lineage,
    )


def map_legacy_fact_type(fact_type: str) -> tuple[str, str, bool]:
    """Map legacy FactEnvelope fact_type → (family, event_type, force_critical)."""
    critical_prefixes = (
        "intent_",
        "risk_",
        "execution_plan",
        "command_",
        "order_",
        "fill",
        "lifecycle_",
        "persistence_",
        "recovery_",
        "resolution_",
        "n6_",
        "n7_",
        "preflight",
        "ptb",
        "kill_switch",
        "mutation",
    )
    force = fact_type.startswith(critical_prefixes) or fact_type in {
        "runtime_start",
        "runtime_stop",
        "failure",
        "market_resolved",
    }
    if fact_type in {"freshness_assessment"}:
        return EventFamily.DATA_HEALTH.value, f"legacy.{fact_type}", False
    if fact_type == "indicator_result":
        return EventFamily.INDICATOR.value, f"legacy.{fact_type}", False
    if fact_type == "signal":
        return EventFamily.SIGNAL.value, f"legacy.{fact_type}", False
    if fact_type == "observe_decision" or fact_type.startswith("zgap_"):
        return EventFamily.DECISION.value, f"legacy.{fact_type}", False
    if "risk" in fact_type:
        return EventFamily.RISK.value, f"legacy.{fact_type}", True
    if "intent" in fact_type:
        return EventFamily.INTENT.value, f"legacy.{fact_type}", True
    if "plan" in fact_type or "execution_plan" in fact_type:
        return EventFamily.PLAN.value, f"legacy.{fact_type}", True
    if "lifecycle" in fact_type:
        return EventFamily.LIFECYCLE.value, f"legacy.{fact_type}", True
    if force:
        return EventFamily.OPERATIONAL.value, f"legacy.{fact_type}", True
    return EventFamily.OPERATIONAL.value, f"legacy.{fact_type}", False
