"""Z-Gap observe-only fact emission with dedup (A0.5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from tyrex_pm.core.ids import RunId
from tyrex_pm.quant.binary_fair_value import FairValueSnapshot
from tyrex_pm.quant.edge import EdgeSnapshot
from tyrex_pm.quant.fees import FeeModel
from tyrex_pm.quant.volatility import VolatilitySnapshot
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_BASIS_COMPUTED,
    FACT_TYPE_EDGE_EVALUATED,
    FACT_TYPE_FEE_MODEL_RESOLVED,
    FACT_TYPE_MODEL_STATE_SNAPSHOT,
    FACT_TYPE_PRICE_TO_BEAT_OBSERVED,
    FACT_TYPE_SIGNAL_FEED_HEALTH,
    FACT_TYPE_Z_GAP_ENTRY_EVAL,
    FACT_TYPE_Z_GAP_ENTRY_SKIP,
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
