"""Minimal signal envelope. Concrete signals (e.g. DirectionalSignal) arrive in R3."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import CorrelationId, EventId


@dataclass(frozen=True, slots=True, kw_only=True)
class Signal:
    """Typed interpretation of market information — not an order request."""

    signal_type: str
    observed_at: datetime
    correlation_id: CorrelationId
    evidence: Mapping[str, Any] = field(default_factory=dict)
    causation_id: EventId | None = None

    def __post_init__(self) -> None:
        if not self.signal_type.strip():
            raise ValueError("signal_type must be non-empty")
        object.__setattr__(
            self, "observed_at", require_utc(self.observed_at, field_name="observed_at")
        )
        object.__setattr__(self, "evidence", dict(self.evidence))
