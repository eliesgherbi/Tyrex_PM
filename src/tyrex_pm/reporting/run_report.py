"""Failure-resilient unified run-report projection."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from tyrex_pm.execution.session_state import ExecutionPhase, ExecutionSessionState
from tyrex_pm.persistence.run_evidence_journal import RunEvidenceRecord

_STRATEGY_INPUT_BLOCKERS = frozenset(
    {
        "TIME_NOT_READY",
        "PTB_NOT_USABLE",
        "FEE_NOT_READY",
        "MODEL_NOT_READY",
        "BASIS_REJECT",
        "JUMP_GUARD",
        "TAU_OUT_OF_BAND",
        "ABS_Z_BLOCK",
        "Z_OUT_OF_BAND",
    }
)


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def execution_state_payload(state: ExecutionSessionState | None) -> dict[str, Any] | None:
    if state is None:
        return None
    return {
        "session_id": state.session_id,
        "phase": state.phase.value,
        "market_id": None if state.identity is None else state.identity.market_id,
        "window_id": None if state.identity is None else state.identity.window_id,
        "token_id": None if state.identity is None else state.identity.token_id,
        "confirmed_position_shares": str(state.confirmed_position_shares),
        "sellable_shares": str(state.sellable_shares),
        "open_venue_order_ids": list(state.open_venue_order_ids),
        "entry_order_ids": list(state.entry_order_ids),
        "exit_order_ids": list(state.exit_order_ids),
        "trade_ids": sorted(state.trades),
        "reconciliation_complete": state.reconciliation_complete,
        "reconciliation_notes": list(state.reconciliation_notes),
        "last_error": state.last_error,
        "event_count": state.event_count,
        "orders": [
            {
                "order_id": order_id,
                "role": order.role.value,
                "dispatch_authorized": order.dispatch_authorized,
                "mutation_attempts": len(order.attempt_ids),
                "pre_dispatch_stage": order.pre_dispatch_stage,
                "pre_dispatch_error_code": order.pre_dispatch_error_code,
                "requested_protection_price": (
                    None
                    if order.requested_protection_price is None
                    else str(order.requested_protection_price)
                ),
                "effective_protection_price": (
                    None
                    if order.effective_protection_price is None
                    else str(order.effective_protection_price)
                ),
                "tick_size": None if order.tick_size is None else str(order.tick_size),
                "last_error": order.last_error,
            }
            for order_id, order in state.orders.items()
        ],
    }


def _outcome(
    state: ExecutionSessionState | None,
    *,
    fatal_error: Mapping[str, Any] | None,
    reporting_failures: Sequence[str],
    runtime_errors: Sequence[str],
    run_evidence: Sequence[RunEvidenceRecord],
) -> tuple[str, bool]:
    if fatal_error is not None:
        return "RUNTIME_FAILURE", False
    if reporting_failures:
        return "REPORTING_DEGRADED", False
    if runtime_errors:
        return "RUNTIME_DEGRADED", False
    if state is None:
        diagnosis = _no_entry_diagnosis(run_evidence)
        if diagnosis["intent_emitted_count"]:
            return "ENTRY_ABORTED_BEFORE_SESSION", False
        if not diagnosis["execution_infrastructure_ready_ever"]:
            return "NO_ENTRY_RUNTIME_BLOCKED", False
        if not diagnosis["strategy_inputs_eligible_ever"]:
            return "NO_ENTRY_STRATEGY_INPUT_BLOCKED", False
        if diagnosis["strategy_inputs_eligible_ever"]:
            return "COMPLETED_NO_ENTRY_SIGNAL", True
        return "NO_ENTRY_RUNTIME_BLOCKED", False  # defensive unreachable fallback
    if state.phase is ExecutionPhase.COMPLETED_NO_DISPATCH:
        return "ENTRY_PRE_DISPATCH_FAILED", False
    if state.phase is ExecutionPhase.COMPLETED_NO_FILL:
        return "COMPLETED_NO_FILL", True
    if state.phase is ExecutionPhase.COMPLETED_FLAT:
        return "COMPLETED_FLAT", True
    if state.phase is ExecutionPhase.MANUAL_INTERVENTION:
        return "MANUAL_INTERVENTION_REQUIRED", False
    if state.has_exposure:
        return "OPEN_EXPOSURE", False
    return "INCOMPLETE", False


def _infrastructure_ready_from_payload(payload: Mapping[str, Any]) -> bool:
    """Prefer the explicit infrastructure fact; fall back for older evidence."""
    if "execution_infrastructure_ready" in payload:
        return bool(payload.get("execution_infrastructure_ready"))
    if payload.get("entry_executable"):
        return True
    blockers = {str(value) for value in payload.get("blockers", ())}
    # Legacy readiness conflated model lockouts with infrastructure. If the
    # only recorded blockers are strategy-input codes, infrastructure was up.
    return bool(blockers) and blockers.issubset(_STRATEGY_INPUT_BLOCKERS)


def _no_entry_diagnosis(records: Sequence[RunEvidenceRecord]) -> dict[str, Any]:
    """Explain why a run with no execution session produced no order.

    The distinction is operationally important: a strategy that declined an
    executable opportunity is a successful no-signal run; a runtime that never
    became executable is not. Strategy-input lockouts (model/vol/PTB/time) are
    not infrastructure failures.
    """
    readiness = [record for record in records if record.event_type == "READINESS_CHANGED"]
    infrastructure_ready_ever = any(
        _infrastructure_ready_from_payload(record.payload) for record in readiness
    )
    entry_executable_ever = any(
        bool(record.payload.get("entry_executable")) for record in readiness
    )
    decision_ready_ever = any(bool(record.payload.get("decision_ready")) for record in readiness)
    intent_count = sum(record.event_type == "INTENT_EMITTED" for record in records)
    decisions = [record for record in records if record.event_type == "STRATEGY_DECISION"]
    decision_count = len(decisions)
    strategy_eligible_count = sum(_decision_inputs_eligible(record) for record in decisions)
    strategy_blockers: Counter[str] = Counter()
    for record in decisions:
        blockers = record.payload.get("strategy_blockers")
        if not isinstance(blockers, (list, tuple)):
            reason = str(record.payload.get("reason_code", "UNKNOWN"))
            blockers = (reason,) if reason in _STRATEGY_INPUT_BLOCKERS else ()
        strategy_blockers.update(str(value) for value in blockers)
    last_readiness = readiness[-1].payload if readiness else {}
    if not infrastructure_ready_ever:
        reason = "RUNTIME_NEVER_ENTRY_EXECUTABLE"
        furthest_stage = "INFRASTRUCTURE"
    elif intent_count == 0:
        if strategy_eligible_count:
            reason = "STRATEGY_EVALUATED_NO_ECONOMIC_SIGNAL"
            furthest_stage = "ECONOMIC_DECISION"
        else:
            reason = "STRATEGY_INPUTS_NEVER_ELIGIBLE"
            furthest_stage = "STRATEGY_INPUT_QUALITY"
    else:
        reason = "ENTRY_INTENT_DID_NOT_CREATE_SESSION"
        furthest_stage = "ENTRY_INTENT"
    return {
        "reason": reason,
        "furthest_stage": furthest_stage,
        "execution_infrastructure_ready_ever": infrastructure_ready_ever,
        "entry_executable_ever": entry_executable_ever,
        "decision_ready_ever": decision_ready_ever,
        "strategy_inputs_eligible_ever": bool(strategy_eligible_count),
        "strategy_inputs_eligible_count": strategy_eligible_count,
        "strategy_decision_count": decision_count,
        "intent_emitted_count": intent_count,
        "strategy_input_blocker_counts": dict(sorted(strategy_blockers.items())),
        "last_readiness_blockers": list(last_readiness.get("blockers", ())),
    }


def _decision_inputs_eligible(record: RunEvidenceRecord) -> bool:
    """Read the explicit v3 fact, with a conservative v2 compatibility rule."""
    if "strategy_inputs_eligible" in record.payload:
        return bool(record.payload.get("strategy_inputs_eligible"))
    reason = str(record.payload.get("reason_code", "UNKNOWN"))
    return reason not in _STRATEGY_INPUT_BLOCKERS


def _run_evidence_summary(records: Sequence[RunEvidenceRecord]) -> dict[str, Any]:
    event_counts = Counter(record.event_type for record in records)
    decision_reasons = Counter(
        str(record.payload.get("reason_code", "UNKNOWN"))
        for record in records
        if record.event_type == "STRATEGY_DECISION"
    )
    skipped_reasons: Counter[str] = Counter()
    blocker_counts: Counter[str] = Counter()
    strategy_blocker_counts: Counter[str] = Counter()
    strategy_eligible_count = 0
    for record in records:
        if record.event_type == "EVALUATION_SKIPPED":
            skipped_reasons.update(str(value) for value in record.payload.get("reasons", ()))
        if record.event_type == "READINESS_CHANGED":
            blocker_counts.update(str(value) for value in record.payload.get("blockers", ()))
        if record.event_type == "STRATEGY_DECISION":
            strategy_eligible_count += int(_decision_inputs_eligible(record))
            blockers = record.payload.get("strategy_blockers", ())
            if isinstance(blockers, (list, tuple)):
                strategy_blocker_counts.update(str(value) for value in blockers)
    return {
        "event_counts": dict(sorted(event_counts.items())),
        "strategy_reason_counts": dict(sorted(decision_reasons.items())),
        "evaluation_skip_reason_counts": dict(sorted(skipped_reasons.items())),
        "readiness_blocker_counts": dict(sorted(blocker_counts.items())),
        "strategy_input_eligible_decision_count": strategy_eligible_count,
        "strategy_input_blocker_counts": dict(sorted(strategy_blocker_counts.items())),
    }


@dataclass(frozen=True)
class RunReportInput:
    run_instance_id: str
    configured_run_name: str
    live_requested: bool
    state: ExecutionSessionState | None
    mutation_attempts: int | None
    execution_timeline: Sequence[Mapping[str, Any]]
    capabilities: Mapping[str, Any]
    market_data: Mapping[str, Any] | None
    run_evidence: Sequence[RunEvidenceRecord]
    runtime_errors: Sequence[str]
    fatal_error: Mapping[str, Any] | None = None
    reporting_failures: Sequence[str] = ()


def build_run_report(source: RunReportInput) -> dict[str, Any]:
    """Build a pure projection; this function never reads a live component."""
    outcome, ok = _outcome(
        source.state,
        fatal_error=source.fatal_error,
        reporting_failures=source.reporting_failures,
        runtime_errors=source.runtime_errors,
        run_evidence=source.run_evidence,
    )
    records = tuple(source.run_evidence)
    no_entry = _no_entry_diagnosis(records) if source.state is None else None
    execution_stage = _execution_stage(source.state)
    return {
        "schema_version": 4,
        "generated_at": _utc_now_text(),
        "run_instance_id": source.run_instance_id,
        "configured_run_name": source.configured_run_name,
        "outcome": outcome,
        "ok": ok,
        "live_requested": source.live_requested,
        "mutation_attempts": (
            None if source.mutation_attempts is None else int(source.mutation_attempts)
        ),
        "execution": execution_state_payload(source.state),
        "no_entry_diagnosis": no_entry,
        "validation_scope": {
            "execution_lifecycle_exercised": source.state is not None,
            "furthest_execution_stage": execution_stage,
            "entry_intent_observed": any(
                record.event_type == "INTENT_EMITTED" for record in records
            ),
            "venue_mutation_attempted": bool(source.mutation_attempts),
            "entry_http_post_attempted": bool(
                source.state is not None
                and source.state.entry is not None
                and source.state.entry.attempt_ids
            ),
            "exit_http_post_attempted": bool(
                source.state is not None
                and source.state.exit is not None
                and source.state.exit.attempt_ids
            ),
            "note": (_validation_note(source.state, execution_stage)),
        },
        "execution_timeline": [dict(row) for row in source.execution_timeline],
        "capabilities": dict(source.capabilities),
        "market_data": None if source.market_data is None else dict(source.market_data),
        "run_evidence_summary": _run_evidence_summary(records),
        "run_evidence": [record.to_dict() for record in records],
        "runtime_errors": list(source.runtime_errors),
        "fatal_error": None if source.fatal_error is None else dict(source.fatal_error),
        "reporting": {
            "degraded": bool(source.reporting_failures),
            "failures": list(source.reporting_failures),
            "execution_authority": "sqlite.execution_events",
            "explanation_evidence": "sqlite.run_events",
        },
    }


def _execution_stage(state: ExecutionSessionState | None) -> str:
    if state is None:
        return "NO_SESSION"
    if state.exit is not None and state.exit.attempt_ids:
        return "EXIT_POST_ATTEMPTED"
    if state.entry is not None and state.entry.attempt_ids:
        return "ENTRY_POST_ATTEMPTED"
    if state.entry is not None and state.entry.pre_dispatch_stage is not None:
        return f"ENTRY_{state.entry.pre_dispatch_stage}_FAILED"
    if state.entry is not None and state.entry.prepared_order_digest is not None:
        return "ENTRY_PREPARED"
    if state.entry is not None:
        return "ENTRY_ORDER_REQUESTED"
    return "SESSION_OPENED"


def _validation_note(state: ExecutionSessionState | None, stage: str) -> str:
    if state is None:
        return "Run stopped before an execution session; BUY/fill/EXIT behavior is untested."
    if not state.mutations_attempted:
        return (
            f"Execution reached {stage}, but no venue POST was attempted; "
            "BUY/fill/EXIT behavior remains untested."
        )
    if state.exit is None or not state.exit.attempt_ids:
        return "Entry POST was attempted; exit submission was not exercised."
    return "Entry and exit venue submission paths were exercised."


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_run_report(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_json_write(path, payload)


def write_emergency_report(
    path: Path,
    *,
    run_instance_id: str,
    configured_run_name: str,
    live_requested: bool,
    mutation_attempts: int | None,
    fatal_error: Mapping[str, Any] | None,
    reporting_error: BaseException,
) -> dict[str, Any]:
    """Write a primitive-only report if normal projection or serialization fails."""
    payload = {
        "schema_version": 4,
        "generated_at": _utc_now_text(),
        "run_instance_id": str(run_instance_id),
        "configured_run_name": str(configured_run_name),
        "outcome": "RUNTIME_FAILURE",
        "ok": False,
        "live_requested": bool(live_requested),
        "mutation_attempts": None if mutation_attempts is None else int(mutation_attempts),
        "execution": None,
        "fatal_error": None if fatal_error is None else dict(fatal_error),
        "reporting": {
            "degraded": True,
            "emergency": True,
            "error_type": type(reporting_error).__name__,
            "error_message": str(reporting_error),
            "execution_authority": "sqlite.execution_events",
        },
    }
    _atomic_json_write(path, payload)
    return payload
