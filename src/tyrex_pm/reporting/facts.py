"""Typed fact builders for the R3 observe chain."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from tyrex_pm.core.facts import FactEnvelope
from tyrex_pm.core.ids import CorrelationId, EventId, RunId, StrategyId
from tyrex_pm.reporting.jsonl import JsonlFactSink

FACT_SCHEMA = JsonlFactSink.SCHEMA_VERSION


def make_fact(
    *,
    fact_type: str,
    ts: datetime,
    run_id: RunId,
    correlation_id: CorrelationId,
    payload: Mapping[str, Any],
    strategy_id: StrategyId | None = None,
    causation_id: EventId | None = None,
) -> FactEnvelope:
    return FactEnvelope(
        fact_type=fact_type,
        schema_version=FACT_SCHEMA,
        ts=ts,
        run_id=run_id,
        correlation_id=correlation_id,
        payload=payload,
        strategy_id=strategy_id,
        causation_id=causation_id,
    )
