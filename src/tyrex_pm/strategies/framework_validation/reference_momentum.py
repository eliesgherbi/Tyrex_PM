"""ReferenceMomentumStrategy — R3 observe decisions + R4 entry transitions.

``evaluate`` is unchanged from R3 (same signal → same ObserveDecision).
Intent emission is a separate transition policy after the observe decision.

Transition policy (R4, no portfolio):
- UNAVAILABLE/FLAT/None → UP/DOWN: emit one EnterIntent
- UP → UP or DOWN → DOWN: suppress (no repeated entry)
- UP ↔ DOWN: record observe decision; no reversal/exit intent until R5
- directional → FLAT/UNAVAILABLE: no entry intent

Strategy owns last direction / last intent metadata only — not orders/fills.
Restart persistence is R5; tests reconstruct strategy state in-process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from tyrex_pm.core.ids import CorrelationId, EventId, StrategyId
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.intents import EnterIntent, new_intent_id
from tyrex_pm.signals.directional import Direction, DirectionalSignal
from tyrex_pm.strategies.context import DecisionContext, StrategyContext


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


@dataclass
class TransitionResult:
    decision: ObserveDecision
    intents: list[EnterIntent] = field(default_factory=list)
    suppressed: bool = False
    suppress_reason: str | None = None


class ReferenceMomentumStrategy:
    STRATEGY_ID = StrategyId("reference_momentum")

    def __init__(self) -> None:
        self._started = False
        self._last_direction: Direction | None = None
        self._last_intent_id: str | None = None
        self._last_intent_at: datetime | None = None
        self._decision_epoch: int = 0

    def on_start(self, context: StrategyContext) -> None:
        self._started = True
        self.reset_for_market()

    def on_stop(self, reason: str) -> None:
        self._started = False

    def reset_for_market(self) -> None:
        """Clear transition state for a new market/window (increments epoch)."""
        self._last_direction = None
        self._last_intent_id = None
        self._last_intent_at = None
        self._decision_epoch += 1

    @property
    def decision_epoch(self) -> int:
        return self._decision_epoch

    @property
    def last_direction(self) -> Direction | None:
        return self._last_direction

    def evaluate(self, signal: DirectionalSignal) -> ObserveDecision:
        """R3 observe decision — must remain mathematically unchanged."""
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

    def on_signal(
        self,
        signal: DirectionalSignal,
        context: DecisionContext,
    ) -> tuple[ObserveDecision, list[EnterIntent]]:
        result = self.apply_transition(signal, context)
        return result.decision, list(result.intents)

    def apply_transition(
        self,
        signal: DirectionalSignal,
        context: DecisionContext,
    ) -> TransitionResult:
        decision = self.evaluate(signal)
        prev = self._last_direction
        new = signal.direction

        if new in (Direction.UP, Direction.DOWN) and prev in (
            None,
            Direction.UNAVAILABLE,
            Direction.FLAT,
        ):
            intent = self._build_enter_intent(signal, context, decision)
            self._last_direction = new
            self._last_intent_id = intent.intent_id.value
            self._last_intent_at = signal.observed_at
            return TransitionResult(decision=decision, intents=[intent])

        if new in (Direction.UP, Direction.DOWN) and prev == new:
            return TransitionResult(
                decision=decision,
                intents=[],
                suppressed=True,
                suppress_reason="REPEATED_DIRECTION",
            )
        if (
            new in (Direction.UP, Direction.DOWN)
            and prev in (Direction.UP, Direction.DOWN)
            and prev != new
        ):
            self._last_direction = new
            return TransitionResult(
                decision=decision,
                intents=[],
                suppressed=True,
                suppress_reason="REVERSAL_NO_PORTFOLIO",
            )
        if new in (Direction.FLAT, Direction.UNAVAILABLE):
            suppress = (
                "EXIT_DEFERRED_TO_R5"
                if prev in (Direction.UP, Direction.DOWN)
                else "NO_ENTRY_TRANSITION"
            )
            self._last_direction = new
            return TransitionResult(
                decision=decision,
                intents=[],
                suppressed=True,
                suppress_reason=suppress,
            )
        self._last_direction = new
        return TransitionResult(
            decision=decision,
            intents=[],
            suppressed=True,
            suppress_reason="NO_ENTRY_TRANSITION",
        )

    def _build_enter_intent(
        self,
        signal: DirectionalSignal,
        context: DecisionContext,
        decision: ObserveDecision,
    ) -> EnterIntent:
        market = context.snapshot.market
        if signal.direction is Direction.UP:
            instrument = market.yes
            outcome = OutcomeSide.YES
        else:
            instrument = market.no
            outcome = OutcomeSide.NO
        return EnterIntent(
            intent_id=new_intent_id(),
            strategy_id=self.STRATEGY_ID,
            instrument_id=instrument.instrument_id,
            market_id=market.market_id,
            created_at=signal.observed_at,
            correlation_id=signal.correlation_id,
            causation_id=signal.causation_id,
            reason_code=decision.reason_code,
            evidence={
                "observe_kind": decision.kind.value,
                "signal_direction": signal.direction.value,
                "decision_epoch": self._decision_epoch,
            },
            target_notional=context.target_notional,
            outcome=outcome,
            decision_epoch=self._decision_epoch,
            max_price=context.max_price,
        )
