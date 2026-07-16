"""Fact envelope for audit reconstruction. JSONL sink arrives in R3."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import CorrelationId, EventId, RunId, StrategyId


@dataclass(frozen=True, slots=True, kw_only=True)
class FactEnvelope:
    """Schema-versioned fact payload without a large taxonomy in R2."""

    fact_type: str
    schema_version: int
    ts: datetime
    run_id: RunId
    correlation_id: CorrelationId
    payload: Mapping[str, Any] = field(default_factory=dict)
    strategy_id: StrategyId | None = None
    causation_id: EventId | None = None

    def __post_init__(self) -> None:
        if not self.fact_type.strip():
            raise ValueError("fact_type must be non-empty")
        if self.schema_version < 1:
            raise ValueError("schema_version must be >= 1")
        object.__setattr__(self, "ts", require_utc(self.ts, field_name="ts"))
        object.__setattr__(self, "payload", dict(self.payload))
