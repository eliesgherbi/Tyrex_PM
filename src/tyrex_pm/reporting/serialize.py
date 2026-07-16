"""JSON-safe serialization for FactEnvelope payloads."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from tyrex_pm.core.facts import FactEnvelope
from tyrex_pm.core.ids import CorrelationId, EventId, InstrumentId, MarketId, RunId, StrategyId, TokenId


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # Avoid float in facts; stringify if it slips through
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(
        value, (CorrelationId, EventId, InstrumentId, MarketId, RunId, StrategyId, TokenId)
    ):
        return value.value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    raise TypeError(f"cannot serialize type {type(value)!r}")


def fact_to_jsonable(fact: FactEnvelope) -> dict[str, Any]:
    return {
        "fact_type": fact.fact_type,
        "schema_version": fact.schema_version,
        "ts": fact.ts.isoformat(),
        "run_id": fact.run_id.value,
        "correlation_id": fact.correlation_id.value,
        "causation_id": None if fact.causation_id is None else fact.causation_id.value,
        "strategy_id": None if fact.strategy_id is None else fact.strategy_id.value,
        "payload": to_jsonable(dict(fact.payload)),
    }
