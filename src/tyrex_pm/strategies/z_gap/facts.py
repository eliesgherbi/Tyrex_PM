"""Z-Gap observe-only fact emission with dedup (A0.5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from tyrex_pm.core.ids import RunId
from tyrex_pm.quant.model_sanity import MODEL_NUMERIC_ANOMALY, ModelSanityResult
from tyrex_pm.quant.edge import EdgeSnapshot
from tyrex_pm.quant.fees import FeeModel
from tyrex_pm.quant.volatility import VolatilitySnapshot
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_BASIS_COMPUTED,
    FACT_TYPE_EDGE_EVALUATED,
    FACT_TYPE_FEE_MODEL_RESOLVED,
    FACT_TYPE_MODEL_STATE_SNAPSHOT,
    FACT_TYPE_MODEL_NUMERIC_ANOMALY,
    FACT_TYPE_PRICE_TO_BEAT_OBSERVED,
    FACT_TYPE_SIGNAL_FEED_HEALTH,
    FACT_TYPE_Z_GAP_ENTRY_BLOCKED,
    FACT_TYPE_Z_GAP_ENTRY_REJECTED,
    FACT_TYPE_Z_GAP_ENTRY_PARTIAL_FILL,
    FACT_TYPE_Z_GAP_ENTRY_EXECUTION_UNRESOLVED,
    FACT_TYPE_Z_GAP_PTB_SOURCE_SELECTED,
    FACT_TYPE_Z_GAP_PTB_SOURCE_MISMATCH,
    FACT_TYPE_Z_GAP_PTB_LOCKED,
    FACT_TYPE_Z_GAP_ENTRY_EVAL,
    FACT_TYPE_Z_GAP_ENTRY_PLAN,
    FACT_TYPE_Z_GAP_ENTRY_SKIP,
    FACT_TYPE_Z_GAP_ENTRY_SUBMIT_READY,
    FACT_TYPE_Z_GAP_ENTRY_SUBMITTED,
    FACT_TYPE_Z_GAP_ENTRY_FILL,
    FACT_TYPE_Z_GAP_ENTRY_UNFILLED,
    FACT_TYPE_Z_GAP_POSITION_ACTIVATED,
    FACT_TYPE_MODEL_EXIT_TRIGGERED,
    FACT_TYPE_Z_GAP_EXIT_SUBMITTED,
    FACT_TYPE_Z_GAP_EXIT_FILL,
    FACT_TYPE_Z_GAP_EXIT_UNFILLED,
    FACT_TYPE_Z_GAP_EXIT_ATTEMPT,
    FACT_TYPE_Z_GAP_POSITION_CLOSED,
    FACT_TYPE_Z_GAP_LIFECYCLE_STATE,
    FACT_TYPE_Z_GAP_MANUAL_INTERVENTION_REQUIRED,
    FACT_TYPE_Z_GAP_POSITION_RECONCILIATION,
    FACT_TYPE_Z_GAP_RECONCILIATION_WARNING,
    FACT_TYPE_Z_GAP_RECONCILIATION_FAILED,
    FACT_TYPE_Z_GAP_TERMINAL_SUMMARY,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.signal_feed_facts import (
    build_basis_computed_payload,
    build_price_to_beat_observed_payload,
    build_signal_feed_health_from_snapshot,
)
from tyrex_pm.runtime.z_gap_model_facts import (
    build_edge_evaluated_payload,
    build_fee_model_resolved_payload,
    build_model_state_snapshot_payload,
)
from tyrex_pm.state.signal_state_store import SignalSnapshot
from tyrex_pm.strategies.z_gap.entry_eval import (
    DECISION_SKIP,
    DECISION_WOULD_ENTER,
    ZGapEntryEvaluation,
)


HEARTBEAT_INTERVAL_S = 30.0


@dataclass
class ZGapObserveRuntimeState:
    """Mutable runtime counters and dedup state for observe-only loop."""

    dedup_keys: set[str] = field(default_factory=set)
    periodic_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    evaluation_count: int = 0
    would_enter_count: int = 0
    skip_reason_histogram: dict[str, int] = field(default_factory=dict)
    basis_status_histogram: dict[str, int] = field(default_factory=dict)
    feed_fresh_ticks: int = 0
    total_ticks: int = 0
    sigma_ready_first_ts: datetime | None = None
    loop_ran: bool = False
    facts_emitted: bool = False
    calibration_sample_written: bool = False
    last_heartbeat_ts: float | None = None
    last_model_key: tuple[Any, ...] | None = None
    last_model_anomaly_key: tuple[Any, ...] | None = None
    last_edge_key: tuple[Any, ...] | None = None
    last_entry_key: tuple[Any, ...] | None = None
    last_fair: FairValueSnapshot | None = None
    last_edge: EdgeSnapshot | None = None
    last_signal: SignalSnapshot | None = None
    last_evaln: ZGapEntryEvaluation | None = None
    ptb_observed: bool = False
    fee_model_status: str | None = None
    fee_model_id: str | None = None


def should_emit_once(state: ZGapObserveRuntimeState, dedup_key: str) -> bool:
    if dedup_key in state.dedup_keys:
        return False
    state.dedup_keys.add(dedup_key)
    return True


def should_emit_periodic(
    state: ZGapObserveRuntimeState,
    *,
    fact_kind: str,
    fingerprint: tuple[Any, ...],
    min_emit_interval_s: float = HEARTBEAT_INTERVAL_S,
) -> bool:
    now = datetime.now(timezone.utc).timestamp()
    cache = state.periodic_cache.get(fact_kind)
    if cache is None:
        state.periodic_cache[fact_kind] = {"ts": now, "fp": fingerprint}
        return True
    last_ts = float(cache["ts"])
    last_fp = cache.get("fp")
    if last_fp != fingerprint:
        state.periodic_cache[fact_kind] = {"ts": now, "fp": fingerprint}
        return True
    if now - last_ts >= min_emit_interval_s:
        state.periodic_cache[fact_kind] = {"ts": now, "fp": fingerprint}
        return True
    return False


def _write(sink: JsonlSink | object | None, obj: dict[str, Any]) -> None:
    if sink is not None:
        sink.write(obj)
        return


SinkLike = JsonlSink | object


def emit_signal_feed_health(
    sink: SinkLike,
    run_id: RunId,
    state: ZGapObserveRuntimeState,
    signal: SignalSnapshot,
) -> None:
    for feed in ("binance", "chainlink"):
        payload = build_signal_feed_health_from_snapshot(signal, feed=feed)
        fp = (feed, payload.get("freshness"), payload.get("stale"), payload.get("price"))
        if not should_emit_periodic(state, fact_kind=f"signal_feed_health:{feed}", fingerprint=fp):
            continue
        _write(sink, make_fact(FACT_TYPE_SIGNAL_FEED_HEALTH, str(run_id), payload))
        state.facts_emitted = True


def emit_basis_computed(
    sink: SinkLike,
    run_id: RunId,
    state: ZGapObserveRuntimeState,
    signal: SignalSnapshot,
) -> None:
    payload = build_basis_computed_payload(signal)
    fp = (payload.get("basis_bps"), payload.get("basis_status"), payload.get("chainlink_freshness"))
    if not should_emit_periodic(state, fact_kind="basis_computed", fingerprint=fp):
        return
    _write(sink, make_fact(FACT_TYPE_BASIS_COMPUTED, str(run_id), payload))
    state.facts_emitted = True
    hist = state.basis_status_histogram
    bs = str(signal.basis_status)
    hist[bs] = hist.get(bs, 0) + 1


def emit_price_to_beat_observed(
    sink: SinkLike,
    run_id: RunId,
    state: ZGapObserveRuntimeState,
    *,
    market_id: str,
    signal: SignalSnapshot,
    event_start_ts: float | None,
    event_end_ts: float | None,
) -> None:
    payload = build_price_to_beat_observed_payload(
        market_id=market_id,
        price_to_beat=str(signal.price_to_beat) if signal.price_to_beat is not None else None,
        ptb_status=signal.ptb_status,
        ptb_lag_ms=signal.ptb_lag_ms,
        event_start_ts=event_start_ts,
        event_end_ts=event_end_ts,
    )
    dedup = f"ptb:{signal.ptb_status}:{payload.get('price_to_beat')}"
    if not should_emit_once(state, dedup):
        return
    _write(sink, make_fact(FACT_TYPE_PRICE_TO_BEAT_OBSERVED, str(run_id), payload))
    state.facts_emitted = True
    if signal.price_to_beat is not None and signal.ptb_status == "observed":
        state.ptb_observed = True


def emit_model_state_snapshot(
    sink: SinkLike,
    run_id: RunId,
    state: ZGapObserveRuntimeState,
    fair: FairValueSnapshot,
    vol: VolatilitySnapshot,
) -> None:
    key = (
        fair.model_status,
        fair.z,
        fair.p_up,
        fair.sigma,
        vol.ready,
        vol.jump_guard_tripped,
    )
    if state.last_model_key == key:
        return
    state.last_model_key = key
    payload = build_model_state_snapshot_payload(fair, vol=vol)
    _write(sink, make_fact(FACT_TYPE_MODEL_STATE_SNAPSHOT, str(run_id), payload))
    state.facts_emitted = True
    state.last_fair = fair


def emit_model_numeric_anomaly(
    sink: SinkLike,
    run_id: RunId,
    state: ZGapObserveRuntimeState,
    fair: FairValueSnapshot,
    sanity: ModelSanityResult,
) -> None:
    key = (sanity.abs_z, sanity.computed_z, tuple(sanity.issues))
    if state.last_model_anomaly_key == key:
        return
    state.last_model_anomaly_key = key
    payload = {
        "anomaly": MODEL_NUMERIC_ANOMALY,
        "issues": list(sanity.issues),
        "abs_z": sanity.abs_z,
        "computed_z": sanity.computed_z,
        "reported_z": fair.z,
        "sigma": fair.sigma,
        "sigma_units": fair.sigma_units,
        "tau_s": fair.tau_s,
        "S": str(fair.S) if fair.S is not None else None,
        "K": str(fair.K) if fair.K is not None else None,
        "p_up": fair.p_up,
        "p_down": fair.p_down,
        "block_entry": sanity.block_entry,
        "snapshot_ts": fair.snapshot_ts.isoformat(),
    }
    _write(sink, make_fact(FACT_TYPE_MODEL_NUMERIC_ANOMALY, str(run_id), payload))
    state.facts_emitted = True


def emit_fee_model_resolved(
    sink: SinkLike,
    run_id: RunId,
    state: ZGapObserveRuntimeState,
    fee_model: FeeModel,
    *,
    price: Any = None,
    condition_id: str | None = None,
    market_id: str | None = None,
) -> None:
    dedup = f"fee:{fee_model.fee_model_status}:{fee_model.fd_r}:{fee_model.fd_e}"
    if not should_emit_once(state, dedup):
        return
    payload = build_fee_model_resolved_payload(
        fee_model,
        price=price,
        condition_id=condition_id,
        market_id=market_id,
    )
    _write(sink, make_fact(FACT_TYPE_FEE_MODEL_RESOLVED, str(run_id), payload))
    state.facts_emitted = True
    state.fee_model_status = fee_model.fee_model_status
    state.fee_model_id = fee_model.fee_model_id


def emit_edge_evaluated(
    sink: SinkLike,
    run_id: RunId,
    state: ZGapObserveRuntimeState,
    fair: FairValueSnapshot,
    edge: EdgeSnapshot,
) -> None:
    key = (
        edge.edge_status,
        str(edge.edge_up),
        str(edge.edge_down),
        edge.selected_leg,
        str(edge.selected_edge),
    )
    if state.last_edge_key == key:
        return
    state.last_edge_key = key
    payload = build_edge_evaluated_payload(fair, edge)
    _write(sink, make_fact(FACT_TYPE_EDGE_EVALUATED, str(run_id), payload))
    state.facts_emitted = True
    state.last_edge = edge


def _entry_payload(evaln: ZGapEntryEvaluation) -> dict[str, Any]:
    fair = evaln.fair_value_snapshot
    edge = evaln.edge_snapshot
    return {
        "decision_status": evaln.decision_status,
        "selected_leg": evaln.selected_leg,
        "selected_edge": str(evaln.selected_edge) if evaln.selected_edge is not None else None,
        "reason_code": evaln.reason_code,
        "gate_results": evaln.gate_results,
        "p_up": fair.p_up if fair is not None else None,
        "p_down": fair.p_down if fair is not None else None,
        "z": fair.z if fair is not None else None,
        "tau_s": fair.tau_s if fair is not None else None,
        "edge_up": str(edge.edge_up) if edge and edge.edge_up is not None else None,
        "edge_down": str(edge.edge_down) if edge and edge.edge_down is not None else None,
        "basis_status": evaln.signal_snapshot.basis_status if evaln.signal_snapshot else None,
        "book_snapshot_summary": evaln.book_snapshot_summary,
        "decision_ts": evaln.decision_ts.isoformat(),
    }


def emit_entry_decision(
    sink: SinkLike,
    run_id: RunId,
    state: ZGapObserveRuntimeState,
    evaln: ZGapEntryEvaluation,
) -> None:
    key = (evaln.decision_status, evaln.reason_code, evaln.selected_leg, str(evaln.selected_edge))
    if state.last_entry_key == key:
        return
    state.last_entry_key = key
    payload = _entry_payload(evaln)
    if evaln.decision_status == DECISION_WOULD_ENTER:
        _write(sink, make_fact(FACT_TYPE_Z_GAP_ENTRY_EVAL, str(run_id), payload))
    elif evaln.decision_status == DECISION_SKIP and evaln.reason_code:
        _write(sink, make_fact(FACT_TYPE_Z_GAP_ENTRY_SKIP, str(run_id), payload))
    else:
        _write(sink, make_fact(FACT_TYPE_Z_GAP_ENTRY_EVAL, str(run_id), payload))
    state.facts_emitted = True
    state.evaluation_count += 1
    state.last_evaln = evaln
    if evaln.decision_status == DECISION_WOULD_ENTER:
        state.would_enter_count += 1
    elif evaln.reason_code:
        hist = state.skip_reason_histogram
        hist[evaln.reason_code] = hist.get(evaln.reason_code, 0) + 1


def emit_terminal_summary(
    sink: SinkLike,
    run_id: RunId,
    state: ZGapObserveRuntimeState,
    *,
    entry_mode: str,
    market_id: str,
    condition_id: str | None,
    signal: SignalSnapshot | None,
    resolved_outcome: str | None = None,
    operational_pass: bool,
) -> None:
    fair = state.last_fair
    edge = state.last_edge
    sigma_ready_s: float | None = None
    if state.sigma_ready_first_ts is not None and signal is not None:
        sigma_ready_s = max(
            0.0,
            (signal.snapshot_ts - state.sigma_ready_first_ts).total_seconds(),
        )
    feed_uptime_pct: float | None = None
    if state.total_ticks > 0:
        feed_uptime_pct = round(100.0 * state.feed_fresh_ticks / state.total_ticks, 2)

    payload = {
        "phase": "A1",
        "entry_mode": entry_mode,
        "market_id": market_id,
        "condition_id": condition_id,
        "ptb_observed": state.ptb_observed,
        "ptb_status": signal.ptb_status if signal else None,
        "ptb_lag_ms": signal.ptb_lag_ms if signal else None,
        "feed_uptime_pct": feed_uptime_pct,
        "sigma_ready_s": sigma_ready_s,
        "fee_model_status": state.fee_model_status,
        "fee_model_id": state.fee_model_id,
        "evaluation_count": state.evaluation_count,
        "would_enter_count": state.would_enter_count,
        "skip_reason_histogram": dict(state.skip_reason_histogram),
        "basis_status_histogram": dict(state.basis_status_histogram),
        "last_z": fair.z if fair else None,
        "last_p_up": fair.p_up if fair else None,
        "last_edge_up": str(edge.edge_up) if edge and edge.edge_up is not None else None,
        "last_edge_down": str(edge.edge_down) if edge and edge.edge_down is not None else None,
        "calibration_sample_written": state.calibration_sample_written,
        "resolved_outcome": resolved_outcome,
        "operational_pass": operational_pass,
    }
    _write(sink, make_fact(FACT_TYPE_Z_GAP_TERMINAL_SUMMARY, str(run_id), payload))
    state.facts_emitted = True


def handle_feed_health_callback(
    sink: SinkLike,
    run_id: RunId,
    state: ZGapObserveRuntimeState,
    payload: dict[str, Any],
) -> None:
    """Route signal_feed_runtime on_health payloads to facts."""
    kind = payload.pop("kind", None)
    if kind == "basis_computed":
        fp = (payload.get("basis_bps"), payload.get("basis_status"))
        if should_emit_periodic(state, fact_kind="basis_computed", fingerprint=fp):
            _write(sink, make_fact(FACT_TYPE_BASIS_COMPUTED, str(run_id), payload))
            state.facts_emitted = True
    elif kind == "price_to_beat_observed":
        dedup = f"ptb_cb:{payload.get('ptb_status')}:{payload.get('price_to_beat')}"
        if should_emit_once(state, dedup):
            _write(sink, make_fact(FACT_TYPE_PRICE_TO_BEAT_OBSERVED, str(run_id), payload))
            state.facts_emitted = True
            if payload.get("price_to_beat") and payload.get("ptb_status") == "observed":
                state.ptb_observed = True


def emit_z_gap_entry_plan_fact(
    sink: SinkLike,
    run_id: RunId,
    plan: Any,
) -> None:
    from tyrex_pm.strategies.z_gap.entry_plan import PLAN_STATUS_BLOCKED, PLAN_STATUS_READY, ZGapEntryPlan

    if not isinstance(plan, ZGapEntryPlan):
        return
    fact_type = FACT_TYPE_Z_GAP_ENTRY_PLAN if plan.plan_status == PLAN_STATUS_READY else FACT_TYPE_Z_GAP_ENTRY_BLOCKED
    _write(sink, make_fact(fact_type, str(run_id), plan.to_fact_payload()))
    if plan.plan_status == PLAN_STATUS_BLOCKED:
        return


def emit_z_gap_entry_submit_ready_fact(
    sink: SinkLike,
    run_id: RunId,
    *,
    plan_payload: dict[str, Any],
    validation_gate_results: dict[str, str],
) -> None:
    payload = {**plan_payload, "validation_gate_results": validation_gate_results}
    _write(sink, make_fact(FACT_TYPE_Z_GAP_ENTRY_SUBMIT_READY, str(run_id), payload))


def emit_z_gap_entry_submitted(
    sink: SinkLike,
    run_id: RunId,
    plan: Any,
    *,
    correlation_id: str,
) -> None:
    payload = {**plan.to_fact_payload(), "correlation_id": correlation_id}
    _write(sink, make_fact(FACT_TYPE_Z_GAP_ENTRY_SUBMITTED, str(run_id), payload))


def emit_z_gap_entry_unfilled(sink: SinkLike, run_id: RunId, lifecycle: Any) -> None:
    _write(sink, make_fact(FACT_TYPE_Z_GAP_ENTRY_UNFILLED, str(run_id), lifecycle.to_dict()))


def emit_z_gap_entry_outcome(sink: SinkLike, run_id: RunId, outcome: Any) -> None:
    from tyrex_pm.strategies.z_gap.execution_outcomes import (
        OUTCOME_BLOCKED,
        OUTCOME_FULL_FILL,
        OUTCOME_PARTIAL_FILL,
        OUTCOME_REJECTED,
        OUTCOME_UNRESOLVED,
        OUTCOME_ZERO_FILL,
    )

    payload = outcome.to_fact_payload()
    category = payload.get("category")
    mapping = {
        OUTCOME_BLOCKED: FACT_TYPE_Z_GAP_ENTRY_BLOCKED,
        OUTCOME_REJECTED: FACT_TYPE_Z_GAP_ENTRY_REJECTED,
        OUTCOME_ZERO_FILL: FACT_TYPE_Z_GAP_ENTRY_UNFILLED,
        OUTCOME_PARTIAL_FILL: FACT_TYPE_Z_GAP_ENTRY_PARTIAL_FILL,
        OUTCOME_FULL_FILL: FACT_TYPE_Z_GAP_ENTRY_FILL,
        OUTCOME_UNRESOLVED: FACT_TYPE_Z_GAP_ENTRY_EXECUTION_UNRESOLVED,
    }
    fact_type = mapping.get(str(category), FACT_TYPE_Z_GAP_ENTRY_EXECUTION_UNRESOLVED)
    _write(sink, make_fact(fact_type, str(run_id), payload))


def emit_z_gap_ptb_source_selected(sink: SinkLike, run_id: RunId, payload: dict[str, Any]) -> None:
    _write(sink, make_fact(FACT_TYPE_Z_GAP_PTB_SOURCE_SELECTED, str(run_id), payload))


def emit_z_gap_ptb_source_mismatch(sink: SinkLike, run_id: RunId, payload: dict[str, Any]) -> None:
    _write(sink, make_fact(FACT_TYPE_Z_GAP_PTB_SOURCE_MISMATCH, str(run_id), payload))


def emit_z_gap_ptb_locked(sink: SinkLike, run_id: RunId, payload: dict[str, Any]) -> None:
    _write(sink, make_fact(FACT_TYPE_Z_GAP_PTB_LOCKED, str(run_id), payload))


def emit_z_gap_entry_fill(sink: SinkLike, run_id: RunId, lifecycle: Any, *, snapshot: Any) -> None:
    payload = {
        **lifecycle.to_dict(),
        "filled_shares": str(snapshot.filled_qty),
        "avg_fill_price": str(lifecycle.entry_avg_price) if lifecycle.entry_avg_price else None,
    }
    _write(sink, make_fact(FACT_TYPE_Z_GAP_ENTRY_FILL, str(run_id), payload))


def emit_z_gap_position_activated(sink: SinkLike, run_id: RunId, lifecycle: Any) -> None:
    payload = {
        "requested_shares": str(lifecycle.entry_requested_shares),
        "filled_shares": str(lifecycle.entry_filled_shares),
        "avg_fill_price": str(lifecycle.entry_avg_price) if lifecycle.entry_avg_price else None,
        "fee": str(lifecycle.entry_fee) if lifecycle.entry_fee else None,
        "active_quantity": str(lifecycle.active_quantity),
        "selected_leg": lifecycle.selected_leg,
        "token_id": lifecycle.token_id,
        "entry_model_p": str(lifecycle.entry_model_p) if lifecycle.entry_model_p else None,
        "entry_z": str(lifecycle.entry_z) if lifecycle.entry_z else None,
        "entry_edge": str(lifecycle.entry_edge) if lifecycle.entry_edge else None,
    }
    _write(sink, make_fact(FACT_TYPE_Z_GAP_POSITION_ACTIVATED, str(run_id), payload))


def emit_model_exit_triggered(
    sink: SinkLike,
    run_id: RunId,
    lifecycle: Any,
    decision: Any,
    *,
    tau_s: float | None,
) -> None:
    payload = {
        "exit_reason": decision.exit_reason,
        "kill_reason": decision.kill_reason,
        "held_leg": decision.held_leg or lifecycle.selected_leg,
        "current_z": str(decision.current_z) if decision.current_z is not None else None,
        "z_stop": str(decision.z_stop) if decision.z_stop is not None else None,
        "confirmation_duration": decision.confirmation_duration_s,
        "active_quantity": str(lifecycle.active_quantity),
        "tau_s": tau_s,
    }
    _write(sink, make_fact(FACT_TYPE_MODEL_EXIT_TRIGGERED, str(run_id), payload))


def emit_z_gap_exit_attempt(sink: SinkLike, run_id: RunId, lifecycle: Any, *, attempt_number: int) -> None:
    payload = {
        "attempt_number": attempt_number,
        "exit_reason": lifecycle.exit_reason,
        "requested_exit_quantity": str(lifecycle.active_quantity),
        "active_quantity": str(lifecycle.active_quantity),
    }
    _write(sink, make_fact(FACT_TYPE_Z_GAP_EXIT_ATTEMPT, str(run_id), payload))


def emit_z_gap_exit_submitted(
    sink: SinkLike,
    run_id: RunId,
    lifecycle: Any,
    *,
    correlation_id: str,
) -> None:
    payload = {
        **lifecycle.to_dict(),
        "correlation_id": correlation_id,
        "requested_exit_quantity": str(lifecycle.exit_requested_shares),
    }
    _write(sink, make_fact(FACT_TYPE_Z_GAP_EXIT_SUBMITTED, str(run_id), payload))


def emit_z_gap_exit_unfilled(sink: SinkLike, run_id: RunId, lifecycle: Any) -> None:
    payload = {
        "exit_reason": lifecycle.exit_reason,
        "requested_exit_quantity": str(lifecycle.exit_requested_shares),
        "remaining_quantity": str(lifecycle.active_quantity),
        "attempt_number": lifecycle.exit_attempts,
    }
    _write(sink, make_fact(FACT_TYPE_Z_GAP_EXIT_UNFILLED, str(run_id), payload))


def emit_z_gap_exit_fill(
    sink: SinkLike,
    run_id: RunId,
    lifecycle: Any,
    *,
    filled_qty: Any,
    remaining: Any,
) -> None:
    payload = {
        "exit_reason": lifecycle.exit_reason,
        "filled_exit_quantity": str(filled_qty),
        "remaining_quantity": str(remaining),
        "attempt_number": lifecycle.exit_attempts,
        "avg_exit_price": str(lifecycle.exit_avg_price) if lifecycle.exit_avg_price else None,
    }
    _write(sink, make_fact(FACT_TYPE_Z_GAP_EXIT_FILL, str(run_id), payload))


def emit_z_gap_position_closed(sink: SinkLike, run_id: RunId, lifecycle: Any) -> None:
    _write(sink, make_fact(FACT_TYPE_Z_GAP_POSITION_CLOSED, str(run_id), lifecycle.to_dict()))


def emit_z_gap_lifecycle_state(
    sink: SinkLike,
    run_id: RunId,
    lifecycle: Any,
    *,
    event: str | None = None,
) -> None:
    payload = lifecycle.to_dict()
    if event:
        payload["event"] = event
    _write(sink, make_fact(FACT_TYPE_Z_GAP_LIFECYCLE_STATE, str(run_id), payload))


def emit_z_gap_manual_intervention_required(sink: SinkLike, run_id: RunId, lifecycle: Any) -> None:
    payload = {
        **lifecycle.to_dict(),
        "manual_intervention_required": True,
        "failure_reason": lifecycle.failure_reason,
    }
    _write(sink, make_fact(FACT_TYPE_Z_GAP_MANUAL_INTERVENTION_REQUIRED, str(run_id), payload))


def emit_z_gap_enforce_terminal_summary(
    sink: SinkLike,
    run_id: RunId,
    state: ZGapObserveRuntimeState,
    *,
    lifecycle: Any | None,
    entry_mode: str,
    market_id: str,
    condition_id: str | None,
    signal: SignalSnapshot | None,
    operational_pass: bool,
    allocation_zero: bool,
    venue_reported_quantity: Decimal | None = None,
    allocated_quantity: Decimal | None = None,
    reconciliation_status: str | None = None,
    requested_entry_quantity: Decimal | None = None,
    filled_entry_quantity: Decimal | None = None,
    entry_attempts: int = 0,
) -> None:
    lc = lifecycle
    payload = {
        "phase": "A2",
        "entry_mode": entry_mode,
        "market_id": market_id,
        "condition_id": condition_id,
        "entry_attempted": lc.entry_attempted if lc else False,
        "entry_submitted": lc.entry_submitted if lc else False,
        "entry_attempts": entry_attempts,
        "requested_entry_quantity": str(requested_entry_quantity or (lc.entry_requested_shares if lc else "0")),
        "filled_entry_quantity": str(filled_entry_quantity or (lc.entry_filled_shares if lc else "0")),
        "entry_filled_shares": str(lc.entry_filled_shares) if lc else "0",
        "entry_avg_price": str(lc.entry_avg_price) if lc and lc.entry_avg_price else None,
        "position_activated": lc.position_activated if lc else False,
        "active_quantity": str(lc.active_quantity) if lc else "0",
        "allocated_quantity": str(allocated_quantity if allocated_quantity is not None else (lc.active_quantity if lc else "0")),
        "venue_reported_quantity": str(venue_reported_quantity) if venue_reported_quantity is not None else None,
        "exit_triggered": lc.exit_triggered if lc else False,
        "exit_reason": lc.exit_reason if lc else None,
        "exit_attempts": lc.exit_attempts if lc else 0,
        "exit_filled_shares": str(lc.exit_filled_shares) if lc else "0",
        "remaining_quantity": str(lc.active_quantity) if lc else "0",
        "position_closed": lc.position_closed if lc else False,
        "allocation_zero": allocation_zero,
        "reconciliation_status": reconciliation_status,
        "manual_intervention_required": lc.manual_intervention_required if lc else False,
        "terminal_state": lc.phase.value if lc else "IDLE",
        "operational_pass": operational_pass,
        "entry_outcome_category": getattr(lc, "entry_outcome_category", None),
        "evaluation_count": state.evaluation_count,
        "would_enter_count": state.would_enter_count,
        "ptb_status": signal.ptb_status if signal else None,
    }
    _write(sink, make_fact(FACT_TYPE_Z_GAP_TERMINAL_SUMMARY, str(run_id), payload))
    state.facts_emitted = True


def emit_z_gap_position_reconciliation(sink: SinkLike, run_id: RunId, result: Any) -> None:
    _write(sink, make_fact(FACT_TYPE_Z_GAP_POSITION_RECONCILIATION, str(run_id), result.to_fact_payload()))


def emit_z_gap_reconciliation_warning(sink: SinkLike, run_id: RunId, result: Any) -> None:
    _write(sink, make_fact(FACT_TYPE_Z_GAP_RECONCILIATION_WARNING, str(run_id), result.to_fact_payload()))


def emit_z_gap_reconciliation_failed(sink: SinkLike, run_id: RunId, result: Any) -> None:
    _write(sink, make_fact(FACT_TYPE_Z_GAP_RECONCILIATION_FAILED, str(run_id), result.to_fact_payload()))
