"""Immutable risk decision and per-policy results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping
from uuid import uuid4

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import CorrelationId, EventId
from tyrex_pm.core.intents import IntentId
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.risk.reasons import RiskReason


@dataclass(frozen=True, slots=True)
class DecisionId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("DecisionId must be non-empty")


def new_decision_id() -> DecisionId:
    return DecisionId(str(uuid4()))


@dataclass(frozen=True, kw_only=True)
class PolicyResult:
    policy_id: str
    approved: bool
    reason_code: RiskReason
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", dict(self.evidence))


@dataclass(frozen=True, kw_only=True)
class RiskDecision:
    decision_id: DecisionId
    intent_id: IntentId
    approved: bool
    mode: RuntimeMode
    reason_codes: tuple[RiskReason, ...]
    policy_results: tuple[PolicyResult, ...]
    evaluated_at: datetime
    correlation_id: CorrelationId
    causation_id: EventId | None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    config_fingerprint: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "evaluated_at", require_utc(self.evaluated_at, field_name="evaluated_at")
        )
        object.__setattr__(self, "evidence", dict(self.evidence))
        if self.approved and RiskReason.APPROVED not in self.reason_codes:
            raise ValueError("approved decision must include APPROVED reason")
        if not self.approved and RiskReason.APPROVED in self.reason_codes:
            raise ValueError("denied decision must not include APPROVED")
