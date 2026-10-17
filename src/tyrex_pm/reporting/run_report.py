"""Failure-resilient unified run-report projection."""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from tyrex_pm.execution.evidence import TradeStatus
from tyrex_pm.execution.session_state import ExecutionPhase, ExecutionSessionState
from tyrex_pm.persistence.run_evidence_journal import RunEvidenceRecord

# Legacy v2 journals without strategy_inputs_eligible. v3 decisions carry
# EligibilityFacts; this set is only a fallback for old evidence.
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
        "dust_residual": None
        if state.dust_residual is None
        else {
            "token_id": state.dust_residual.token_id,
            "shares": str(state.dust_residual.shares),
            "market_id": state.dust_residual.market_id,
            "window_id": state.dust_residual.window_id,
            "classification": state.dust_residual.classification,
            "last_bid": (
                None
                if state.dust_residual.last_bid is None
                else str(state.dust_residual.last_bid)
            ),
            "min_order_size": (
                None
                if state.dust_residual.min_order_size is None
                else str(state.dust_residual.min_order_size)
            ),
            "sell_size_increment": (
                None
                if state.dust_residual.sell_size_increment is None
                else str(state.dust_residual.sell_size_increment)
            ),
            "notional": (
                None
                if state.dust_residual.notional is None
                else str(state.dust_residual.notional)
            ),
            "reason": state.dust_residual.reason,
        },
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
                "prepare_queue_wait_ms": order.prepare_queue_wait_ms,
                "tick_resolve_ms": order.tick_resolve_ms,
                "create_market_order_ms": order.create_market_order_ms,
                "refresh_wait_ms": order.refresh_wait_ms,
                "refresh_elapsed_ms": order.refresh_elapsed_ms,
                "order_metadata_cache_miss": order.order_metadata_cache_miss,
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
    startup_outcome: str | None = None,
    mutation_attempts: int | None = None,
    run_sessions: Sequence[ExecutionSessionState] = (),
    committed_session_ids: frozenset[str] = frozenset(),
) -> tuple[str, bool, dict[str, Any] | None]:
    if startup_outcome:
        return startup_outcome, False, None
    if fatal_error is not None:
        return "RUNTIME_FAILURE", False, None
    if reporting_failures:
        return "REPORTING_DEGRADED", False, None
    if runtime_errors:
        return "RUNTIME_DEGRADED", False, None
    if state is None and run_sessions:
        return _continuous_outcome(run_sessions, committed_session_ids)
    if state is None:
        cutoff = _cutoff_only_block(
            mutation_attempts=mutation_attempts,
            run_evidence=run_evidence,
        )
        if cutoff is not None:
            return "NO_ENTRY_CUTOFF_BLOCKED", True, cutoff
        diagnosis = _no_entry_diagnosis(run_evidence)
        if diagnosis["intent_emitted_count"]:
            return "ENTRY_ABORTED_BEFORE_SESSION", False, None
        if not diagnosis["execution_infrastructure_ready_ever"]:
            return "NO_ENTRY_RUNTIME_BLOCKED", False, None
        if not diagnosis["strategy_inputs_eligible_ever"]:
            return "NO_ENTRY_STRATEGY_INPUT_BLOCKED", False, None
        if diagnosis["strategy_inputs_eligible_ever"]:
            return "COMPLETED_NO_ENTRY_SIGNAL", True, None
        return "NO_ENTRY_RUNTIME_BLOCKED", False, None  # defensive unreachable fallback
    if state.phase is ExecutionPhase.COMPLETED_NO_DISPATCH:
        return "COMPLETED_NO_DISPATCH", True, None
    if state.phase is ExecutionPhase.COMPLETED_NO_FILL:
        return "COMPLETED_NO_FILL", True, None
    if state.phase is ExecutionPhase.COMPLETED_FLAT:
        return "COMPLETED_FLAT", True, None
    if state.phase is ExecutionPhase.COMPLETED_FLAT_WITH_DUST:
        residual = state.dust_residual
        return "COMPLETED_FLAT_WITH_DUST", residual is not None, None
    if state.phase is ExecutionPhase.MANUAL_INTERVENTION:
        return "MANUAL_INTERVENTION_REQUIRED", False, None
    if state.has_exposure:
        return "OPEN_EXPOSURE", False, None
    return "INCOMPLETE", False, None


def _cutoff_only_block(
    *,
    mutation_attempts: int | None,
    run_evidence: Sequence[RunEvidenceRecord],
) -> dict[str, Any] | None:
    """Benign no-entry only when every emitted entry was rejected at the hard cutoff.

    A missing mutation count is unknown, not zero. Any other block, a session,
    or a non-entry intent keeps the existing failure classification.
    """
    if mutation_attempts != 0:
        return None
    emitted: list[str] = []
    blocks: list[dict[str, Any]] = []
    for record in run_evidence:
        if record.event_type == "DISPATCH_BLOCKED":
            return None
        if record.event_type == "INTENT_EMITTED":
            if record.payload.get("intent_type") != "EnterIntent":
                return None
            intent_id = record.payload.get("intent_id")
            if not isinstance(intent_id, str) or not intent_id:
                return None
            emitted.append(intent_id)
        elif record.event_type == "INTENT_BLOCKED":
            if record.payload.get("reason") != "ENTRY_CUTOFF_PASSED":
                return None
            if record.payload.get("intent_type") not in (None, "EnterIntent"):
                return None
            parsed = _parse_cutoff_block(record.payload)
            if parsed is None:
                return None
            blocks.append(parsed)
    if not emitted or not blocks:
        return None
    if set(emitted) != {block["intent_id"] for block in blocks}:
        return None
    return blocks[-1]


def _parse_cutoff_block(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    intent_id = payload.get("intent_id")
    tau = payload.get("tau_s")
    cutoff = payload.get("cutoff_s")
    if not isinstance(intent_id, str) or not intent_id:
        return None
    if isinstance(tau, bool) or isinstance(cutoff, bool):
        return None
    if not isinstance(tau, (int, float)) or not isinstance(cutoff, (int, float)):
        return None
    if not math.isfinite(float(tau)) or not math.isfinite(float(cutoff)):
        return None
    missed = float(Decimal(str(cutoff)) - Decimal(str(tau)))
    if missed <= 0:
        return None
    return {
        "intent_id": intent_id,
        "tau_s": float(tau),
        "cutoff_s": float(cutoff),
        "missed_by_s": missed,
        "reason": "ENTRY_CUTOFF_PASSED",
    }


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


def _continuous_outcome(
    sessions: Sequence[ExecutionSessionState], committed: frozenset[str]
) -> tuple[str, bool, None]:
    """Outcome of a continuous run whose sessions were all released or detached."""
    if any(s.phase is ExecutionPhase.MANUAL_INTERVENTION for s in sessions):
        return "MANUAL_INTERVENTION_REQUIRED", False, None
    if any(s.has_exposure and s.session_id not in committed for s in sessions):
        return "OPEN_EXPOSURE", False, None
    return "COMPLETED_CONTINUOUS", True, None


def _confirmed_fills(state: ExecutionSessionState) -> list[dict[str, Any]]:
    return [
        {
            "session_id": state.session_id,
            "venue_trade_id": trade.venue_trade_id,
            "side": trade.side,
            "shares": str(trade.shares),
            "price": str(trade.price),
        }
        for trade in state.trades.values()
        if trade.status is TradeStatus.CONFIRMED
    ]


def _run_sessions_payload(
    sessions: Sequence[ExecutionSessionState], committed: frozenset[str]
) -> dict[str, Any] | None:
    if not sessions:
        return None
    rows = [
        {
            "session_id": s.session_id,
            "window_id": None if s.identity is None else s.identity.window_id,
            "phase": s.phase.value,
            "committed_hold": s.session_id in committed,
            "entry_posts": 0 if s.entry is None else len(s.entry.attempt_ids),
            "exit_posts": 0 if s.exit is None else len(s.exit.attempt_ids),
            "confirmed_position_shares": str(s.confirmed_position_shares),
            "confirmed_fills": len(_confirmed_fills(s)),
        }
        for s in sessions
    ]
    return {
        "session_count": len(rows),
        "entry_posts": sum(r["entry_posts"] for r in rows),
        "exit_posts": sum(r["exit_posts"] for r in rows),
        "confirmed_fills": sum(r["confirmed_fills"] for r in rows),
        "committed_holds": sum(r["committed_hold"] for r in rows),
        "sessions": rows,
    }


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
    prior_session_risk: bool | None = None
    reproducibility: Mapping[str, Any] | None = None
    projection_failed: bool = False
    startup_outcome: str | None = None
    startup_reconciliation: Mapping[str, Any] | None = None
    recovery_sessions: Sequence[Mapping[str, Any]] = ()
    process_mutation_attempts: int | None = None
    strategy_started: bool = True
    # Continuous runs: every session the run opened (all windows, attempts, detached
    # committed holds) and the ids of those committed to resolution (ledger-owned).
    run_sessions: Sequence[ExecutionSessionState] = ()
    committed_session_ids: frozenset[str] = frozenset()


def _fill_completeness(
    state: ExecutionSessionState | None,
    *,
    prior_session_risk: bool | None = None,
    projection_failed: bool = False,
) -> dict[str, Any]:
    if projection_failed:
        return {
            "observed_fills": None,
            "observed_fills_complete": False,
            "reconciliation_complete": None,
            "prior_session_risk": prior_session_risk,
            "fees": None,
            "residual_shares": None,
            "open_venue_order_ids": None,
        }
    if state is None:
        risk = bool(prior_session_risk)
        return {
            "observed_fills": [],
            "observed_fills_complete": not risk,
            "reconciliation_complete": None,
            "prior_session_risk": risk,
            "fees": None,
            "residual_shares": None,
            "open_venue_order_ids": None,
        }
    fills = [
        {
            "venue_trade_id": trade.venue_trade_id,
            "side": trade.side,
            "shares": str(trade.shares),
            "price": str(trade.price),
        }
        for trade in state.trades.values()
        if trade.status is TradeStatus.CONFIRMED
    ]
    pending_or_ambiguous = bool(
        state.has_journal_uncertainty
        or state.has_unresolved_mutation_attempt
        or state.entry_match_awaiting_confirmation
        or state.working_maker_entry
        or state.resting_limit_venue_ids
        or any(trade.status is not TradeStatus.CONFIRMED for trade in state.trades.values())
    )
    if state.phase is ExecutionPhase.MANUAL_INTERVENTION:
        complete = False
        recon: bool | None = False
        residual = None
    elif state.phase is ExecutionPhase.COMPLETED_NO_DISPATCH:
        complete = not pending_or_ambiguous
        recon = True
        residual = None
    else:
        complete = (
            state.phase
            in {
                ExecutionPhase.COMPLETED_NO_FILL,
                ExecutionPhase.COMPLETED_FLAT,
                ExecutionPhase.COMPLETED_FLAT_WITH_DUST,
            }
            and state.reconciliation_complete
            and not pending_or_ambiguous
        )
        recon = state.reconciliation_complete
        residual = (
            None
            if state.dust_residual is None
            else str(state.dust_residual.shares)
        )
        if state.has_exposure and state.phase is not ExecutionPhase.COMPLETED_FLAT_WITH_DUST:
            residual = str(state.confirmed_position_shares)
    risk = True if prior_session_risk is None else bool(prior_session_risk)
    if prior_session_risk is None:
        risk = not state.safe_to_open_entry
    return {
        "observed_fills": fills,
        "observed_fills_complete": complete,
        "reconciliation_complete": recon,
        "prior_session_risk": risk,
        "fees": None,
        "residual_shares": residual,
        "open_venue_order_ids": list(state.open_venue_order_ids),
    }


def build_run_report(source: RunReportInput) -> dict[str, Any]:
    """Build a pure projection; this function never reads a live component."""
    outcome, ok, cutoff_block = _outcome(
        source.state,
        fatal_error=source.fatal_error,
        reporting_failures=source.reporting_failures,
        runtime_errors=source.runtime_errors,
        run_evidence=source.run_evidence,
        startup_outcome=source.startup_outcome,
        mutation_attempts=source.mutation_attempts,
        run_sessions=source.run_sessions,
        committed_session_ids=source.committed_session_ids,
    )
    records = tuple(source.run_evidence)
    sessions = tuple(source.run_sessions)
    no_entry = (
        _no_entry_diagnosis(records) if source.state is None and not sessions else None
    )
    execution_stage = (
        "CONTINUOUS_SESSIONS_RELEASED"
        if source.state is None and sessions
        else _execution_stage(source.state)
    )
    summary = _fill_completeness(
        source.state,
        prior_session_risk=source.prior_session_risk,
        projection_failed=source.projection_failed,
    )
    if source.state is None and sessions and not source.projection_failed:
        summary = {
            **summary,
            "observed_fills": [f for s in sessions for f in _confirmed_fills(s)],
            "reconciliation_complete": all(s.reconciliation_complete for s in sessions),
        }
    return {
        "schema_version": 5,
        "generated_at": _utc_now_text(),
        "run_instance_id": source.run_instance_id,
        "configured_run_name": source.configured_run_name,
        "outcome": outcome,
        "ok": ok,
        "live_requested": source.live_requested,
        "mutation_attempts": (
            None if source.mutation_attempts is None else int(source.mutation_attempts)
        ),
        "process_mutation_attempts": (
            None
            if source.process_mutation_attempts is None
            else int(source.process_mutation_attempts)
        ),
        "execution": execution_state_payload(source.state),
        "no_entry_diagnosis": no_entry,
        "entry_cutoff_block": cutoff_block,
        "startup_reconciliation": (
            None if source.startup_reconciliation is None else dict(source.startup_reconciliation)
        ),
        "recovery_sessions": [dict(item) for item in source.recovery_sessions],
        "run_sessions": _run_sessions_payload(sessions, source.committed_session_ids),
        "strategy_session": (
            None
            if not source.strategy_started or source.state is None
            else source.state.session_id
        ),
        "validation_scope": {
            "execution_lifecycle_exercised": source.state is not None or bool(sessions),
            "furthest_execution_stage": execution_stage,
            "entry_intent_observed": any(
                record.event_type == "INTENT_EMITTED" for record in records
            ),
            "venue_mutation_attempted": bool(source.mutation_attempts),
            "entry_http_post_attempted": any(
                s.entry is not None and bool(s.entry.attempt_ids)
                for s in ((source.state,) if source.state is not None else sessions)
            ),
            "exit_http_post_attempted": any(
                s.exit is not None and bool(s.exit.attempt_ids)
                for s in ((source.state,) if source.state is not None else sessions)
            ),
            "note": _validation_note(source.state, execution_stage, cutoff_block),
        },
        "execution_timeline": [dict(row) for row in source.execution_timeline],
        "capabilities": dict(source.capabilities),
        "market_data": None if source.market_data is None else dict(source.market_data),
        "run_evidence_summary": _run_evidence_summary(records),
        "run_evidence": [record.to_dict() for record in records],
        "runtime_errors": list(source.runtime_errors),
        "fatal_error": None if source.fatal_error is None else dict(source.fatal_error),
        "summary": summary,
        "reproducibility": None if source.reproducibility is None else dict(source.reproducibility),
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


def _validation_note(
    state: ExecutionSessionState | None,
    stage: str,
    cutoff_block: Mapping[str, Any] | None = None,
) -> str:
    if cutoff_block is not None and state is None:
        return (
            "Execution lifecycle was not exercised. "
            "No BUY, fill, protection, or exit was tested. "
            "R7 confirmed-entry/exit acceptance remains incomplete. "
            f"Entry intent {cutoff_block['intent_id']} measured tau_s={cutoff_block['tau_s']} "
            f"against hard cutoff_s={cutoff_block['cutoff_s']} "
            f"and missed the cutoff by {cutoff_block['missed_by_s']} seconds. "
            "Mutation attempts remain zero. "
            "ok true means the runtime completed safely; it does not satisfy "
            "R7 confirmed entry and exit."
        )
    if state is None and stage == "CONTINUOUS_SESSIONS_RELEASED":
        return (
            "Continuous run: every window session was released or detached; "
            "see run_sessions and windows.jsonl for per-window outcomes."
        )
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
    reproducibility: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a primitive-only report if normal projection or serialization fails."""
    repro = None if reproducibility is None else dict(reproducibility)
    if repro is not None:
        repro.pop("effective_config", None)
    payload = {
        "schema_version": 5,
        "generated_at": _utc_now_text(),
        "run_instance_id": str(run_instance_id),
        "configured_run_name": str(configured_run_name),
        "outcome": "RUNTIME_FAILURE",
        "ok": False,
        "live_requested": bool(live_requested),
        "mutation_attempts": None if mutation_attempts is None else int(mutation_attempts),
        "execution": None,
        "fatal_error": None if fatal_error is None else dict(fatal_error),
        "reproducibility": repro,
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
