"""Observe-only ReferenceMomentumStrategy (R3 — no intents)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from tyrex_pm.core.ids import CorrelationId, EventId, StrategyId
from tyrex_pm.signals.directional import Direction, DirectionalSignal


class ObserveDecisionKind(str, Enum):
    WOULD_ENTER_UP = "WOULD_ENTER_UP"
    WOULD_ENTER_DOWN = "WOULD_ENTER_DOWN"
    HOLD = "HOLD"
    SKIP = "SKIP"


@dataclass(frozen=True, kw_only=True)
class ObserveDecision:
    kind: ObserveDecisionKind
    reason_code: str
    observed_at: datetime
    correlation_id: CorrelationId
    causation_id: EventId | None
    signal_direction: Direction
    evidence: dict[str, Any]
    strategy_id: StrategyId = StrategyId("reference_momentum")


class ReferenceMomentumStrategy:
    STRATEGY_ID = StrategyId("reference_momentum")

    def evaluate(self, signal: DirectionalSignal) -> ObserveDecision:
        if signal.direction is Direction.UNAVAILABLE:
            return ObserveDecision(
                kind=ObserveDecisionKind.SKIP,
                reason_code=signal.reason_code,
                observed_at=signal.observed_at,
                correlation_id=signal.correlation_id,
                causation_id=signal.causation_id,
                signal_direction=signal.direction,
                evidence=dict(signal.evidence),
                strategy_id=self.STRATEGY_ID,
            )
        if signal.direction is Direction.UP:
            return ObserveDecision(
                kind=ObserveDecisionKind.WOULD_ENTER_UP,
                reason_code=signal.reason_code,
                observed_at=signal.observed_at,
                correlation_id=signal.correlation_id,
                causation_id=signal.causation_id,
                signal_direction=signal.direction,
                evidence={
                    **signal.evidence,
                    "momentum": None if signal.momentum is None else str(signal.momentum),
                    "strength": None if signal.strength is None else str(signal.strength),
                },
                strategy_id=self.STRATEGY_ID,
            )
        if signal.direction is Direction.DOWN:
            return ObserveDecision(
                kind=ObserveDecisionKind.WOULD_ENTER_DOWN,
                reason_code=signal.reason_code,
                observed_at=signal.observed_at,
                correlation_id=signal.correlation_id,
                causation_id=signal.causation_id,
                signal_direction=signal.direction,
                evidence={
                    **signal.evidence,
                    "momentum": None if signal.momentum is None else str(signal.momentum),
                    "strength": None if signal.strength is None else str(signal.strength),
                },
                strategy_id=self.STRATEGY_ID,
            )
        # FLAT
        return ObserveDecision(
            kind=ObserveDecisionKind.HOLD,
            reason_code=signal.reason_code,
            observed_at=signal.observed_at,
            correlation_id=signal.correlation_id,
            causation_id=signal.causation_id,
            signal_direction=signal.direction,
            evidence={
                **signal.evidence,
                "momentum": None if signal.momentum is None else str(signal.momentum),
            },
            strategy_id=self.STRATEGY_ID,
        )
