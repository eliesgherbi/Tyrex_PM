"""Production wiring for MarketReadinessTracker (M8)."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.execution.planner import build_quality_gate_from_config
from tyrex_pm.market_data.readiness import MarketReadinessTracker
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_MARKET_DATA_HEALTH_BLOCK,
    FACT_TYPE_MARKET_READINESS_TRANSITION,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import AppConfig, PairedBinaryStrategyConfig


def ensure_market_readiness_tracker(
    coord,
    app: AppConfig,
    cfg: PairedBinaryStrategyConfig | None = None,
) -> MarketReadinessTracker | None:
    if not app.runtime.market_data.websocket.primary_enabled:
        return None
    existing = getattr(coord, "market_readiness_tracker", None)
    if existing is not None:
        return existing
    pb = cfg or getattr(app, "paired_binary", None)
    if pb is None:
        return None
    tracker = MarketReadinessTracker(
        yes_token_id=TokenId(pb.yes_token_id),
        no_token_id=TokenId(pb.no_token_id),
    )
    coord.market_readiness_tracker = tracker
    return tracker


def emit_readiness_transitions(
    sink: JsonlSink | None,
    run_id: RunId | str | None,
    tracker: MarketReadinessTracker,
    *,
    correlation_id: str | None = None,
) -> None:
    if sink is None or run_id is None:
        return
    for tr in tracker.drain_transitions():
        payload = dict(tr)
        if correlation_id:
            payload["correlation_id"] = correlation_id
        sink.write(make_fact(FACT_TYPE_MARKET_READINESS_TRANSITION, str(run_id), payload))


def emit_market_data_health_block(
    sink: JsonlSink | None,
    run_id: RunId | str | None,
    *,
    block_reason: str,
    decision_context: str,
    readiness_state: str | None = None,
    quality_verdict: str | None = None,
    quality_reasons: tuple[str, ...] | None = None,
    correlation_id: str | None = None,
) -> None:
    if sink is None or run_id is None:
        return
    payload: dict = {
        "block_reason": block_reason,
        "decision_context": decision_context,
    }
    if readiness_state is not None:
        payload["readiness_state"] = readiness_state
    if quality_verdict is not None:
        payload["quality_verdict"] = quality_verdict
    if quality_reasons:
        payload["quality_reasons"] = list(quality_reasons)
    if correlation_id:
        payload["correlation_id"] = correlation_id
    sink.write(make_fact(FACT_TYPE_MARKET_DATA_HEALTH_BLOCK, str(run_id), payload))


def refresh_market_readiness(
    coord,
    app: AppConfig,
    cfg: PairedBinaryStrategyConfig,
    *,
    sink: JsonlSink | None = None,
    run_id: RunId | str | None = None,
) -> MarketReadinessTracker | None:
    tracker = ensure_market_readiness_tracker(coord, app, cfg)
    if tracker is None or coord.market_state is None:
        return tracker
    gate = build_quality_gate_from_config(app)
    tracker.refresh(
        coord.market_state,
        gate=gate,
        pair_id=cfg.market_id or "paired_binary",
        size=cfg.position_size,
    )
    emit_readiness_transitions(
        sink,
        run_id,
        tracker,
        correlation_id=cfg.market_id,
    )
    return tracker
