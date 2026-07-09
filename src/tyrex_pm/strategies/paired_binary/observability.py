"""Paired-binary material-decision observability (Group C0 / M7 wiring)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.execution.planner import build_quality_gate_from_config
from tyrex_pm.market_data.decision_snapshot import build_decision_snapshot
from tyrex_pm.market_data.quality import DecisionContext
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import AppConfig, PairedBinaryStrategyConfig
from tyrex_pm.strategies.paired_binary import facts as pb_facts
from tyrex_pm.strategies.paired_binary.facts import (
    EventCorrelationContext,
    build_event_correlation_fields,
)
from tyrex_pm.strategies.paired_binary.latency import LatencyTracker
from tyrex_pm.strategies.paired_binary.state import PairedBinaryRuntimeState


def new_decision_id() -> str:
    return str(uuid4())


def trigger_to_context(trigger_type: str) -> DecisionContext:
    if trigger_type == "take_profit":
        return DecisionContext.TAKE_PROFIT
    if trigger_type in ("stop_loss", "stop"):
        return DecisionContext.STOP
    if trigger_type.startswith("survival_"):
        return DecisionContext.URGENT_EXIT
    return DecisionContext.URGENT_EXIT


def emit_material_decision(
    *,
    app: AppConfig,
    coord,
    sink: JsonlSink,
    run_id: RunId,
    cfg: PairedBinaryStrategyConfig,
    state: PairedBinaryRuntimeState,
    decision_type: str,
    context: DecisionContext,
    size: Decimal,
    decision_id: str | None = None,
    latency_tracker: LatencyTracker | None = None,
    emit_latency: bool = True,
    event_correlation: EventCorrelationContext | None = None,
) -> str:
    """Capture pair snapshot, evaluate quality (observe_only), emit Group B facts."""
    did = decision_id or new_decision_id()
    emit_corr = app.runtime.observability.emit_event_correlation
    wall_ts: datetime = utc_now()
    corr_fields = build_event_correlation_fields(
        event_correlation,
        enabled=emit_corr,
        decision_wall_ts=wall_ts,
    )
    if not app.runtime.observability.emit_decision_snapshot:
        if latency_tracker is not None:
            latency_tracker.decision_id = did
        return did

    pair_id = state.pair_correlation_id or "paired_binary_unknown"
    gate = build_quality_gate_from_config(app)
    pair = None
    store = coord.market_state
    if store is not None and hasattr(store, "capture_pair"):
        pair = store.capture_pair(
            TokenId(cfg.yes_token_id),
            TokenId(cfg.no_token_id),
            pair_id,
        )
    report = gate.evaluate_pair(pair, context=context, size=size) if pair is not None else None
    if report is not None:
        pb_facts.emit_data_quality_verdict(
            sink,
            run_id,
            decision_id=did,
            decision_context=context.value,
            report=report,
            correlation_id=state.pair_correlation_id,
        )
    snapshot = build_decision_snapshot(
        decision_type=decision_type,
        pair=pair,
        quality_report=report,
        size=size,
        decision_id=did,
    )
    pb_facts.emit_decision_snapshot(
        sink,
        run_id,
        snapshot=snapshot,
        correlation_id=state.pair_correlation_id,
        extra_payload=corr_fields or None,
    )
    if emit_latency:
        tracker = latency_tracker or LatencyTracker(decision_id=did)
        tracker.decision_id = did
        tracker.mark_decision()
        if pair is not None:
            tracker.book_age_ms = max(pair.yes.book_age_ms, pair.no.book_age_ms)
            tracker.source = pair.yes.source
        if latency_tracker is None or decision_type == "activation":
            pb_facts.emit_latency_chain(
                sink,
                run_id,
                chain=tracker.build_chain(decision_id=did),
                correlation_id=state.pair_correlation_id,
                extra_payload=corr_fields or None,
            )
    return did
