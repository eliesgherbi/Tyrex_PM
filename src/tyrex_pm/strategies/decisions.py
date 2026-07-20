"""Neutral strategy decision contracts (framework-generic).

F1: no validation-strategy types, no Z-Gap concepts, no venue/OMS fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping
from uuid import uuid4

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import CorrelationId, EventId, StrategyId
from tyrex_pm.core.intents import EnterIntent, ExitIntent, FlattenIntent

IntentLike = EnterIntent | ExitIntent | FlattenIntent

# F1 generic economic effects — do not add STOP or HOLD_TO_RESOLUTION here.
_F1_ACTIONS = frozenset(
    {"WAIT", "SKIP", "ENTER", "HOLD", "EXIT", "FLATTEN", "BLOCKED"}
)


class StrategyAction(str, Enum):
    """Generic strategy economic effects (P0/F1 vocabulary)."""

    WAIT = "WAIT"
    SKIP = "SKIP"
    ENTER = "ENTER"
    HOLD = "HOLD"
    EXIT = "EXIT"
    FLATTEN = "FLATTEN"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, kw_only=True)
class StrategyDecision:
    """Neutral strategy evaluation outcome.

    Distinguishes *effect* (``action``) from *why* (``reason_code`` / evidence).
    Exit-family distinctions (thesis, time, rich, …) belong in ``reason_code``,
    not as separate generic actions.
    """

    action: StrategyAction
    reason_code: str
    decided_at: datetime
    correlation_id: CorrelationId
    causation_id: EventId | None = None
    decision_id: str = field(default_factory=lambda: str(uuid4()))
    evidence: Mapping[str, Any] = field(default_factory=dict)
    strategy_id: StrategyId | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decided_at",
            require_utc(self.decided_at, field_name="decided_at"),
        )
        if not self.reason_code.strip():
            raise ValueError("reason_code must be non-empty")
        if not self.decision_id.strip():
            raise ValueError("decision_id must be non-empty")
        if self.action.value not in _F1_ACTIONS:
            raise ValueError(f"unsupported StrategyAction: {self.action!r}")
        object.__setattr__(self, "evidence", dict(self.evidence))
