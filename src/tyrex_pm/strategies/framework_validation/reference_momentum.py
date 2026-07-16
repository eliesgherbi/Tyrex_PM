"""ReferenceMomentumStrategy — R3 observe + R4/R5 transitions.

``evaluate`` is unchanged from R3.
When ``DecisionContext.lifecycle`` is None (R4 dry), signal-transition rules apply.
When lifecycle is provided (R5), eligibility uses authoritative lifecycle/position.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from tyrex_pm.core.ids import CorrelationId, EventId, StrategyId
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.intents import (
    EnterIntent,
    ExitIntent,
    FlattenIntent,
    new_intent_id,
)
from tyrex_pm.lifecycle.trade_lifecycle import LifecycleState
from tyrex_pm.signals.directional import Direction, DirectionalSignal
from tyrex_pm.strategies.context import DecisionContext, StrategyContext

IntentLike = EnterIntent | ExitIntent | FlattenIntent


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
    intents: list[IntentLike] = field(default_factory=list)
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
        self._last_direction = None
        self._last_intent_id = None
        self._last_intent_at = None
        self._decision_epoch += 1

    def bump_decision_epoch(self) -> None:
        """Allow a new entry after lifecycle returns to FLAT."""
        self._decision_epoch += 1

    def restore_state(
        self,
        *,
        decision_epoch: int,
        last_direction: Direction | None,
    ) -> None:
        self._decision_epoch = decision_epoch
        self._last_direction = last_direction

    @property
    def decision_epoch(self) -> int:
        return self._decision_epoch

    @property
    def last_direction(self) -> Direction | None:
        return self._last_direction

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
    ) -> tuple[ObserveDecision, list[IntentLike]]:
        result = self.apply_transition(signal, context)
        return result.decision, list(result.intents)

    def apply_transition(
        self,
        signal: DirectionalSignal,
        context: DecisionContext,
    ) -> TransitionResult:
        decision = self.evaluate(signal)
        if context.lifecycle is not None:
            return self._apply_lifecycle_transition(signal, context, decision)
        return self._apply_signal_transition(signal, context, decision)

    def _apply_lifecycle_transition(
        self,
        signal: DirectionalSignal,
        context: DecisionContext,
        decision: ObserveDecision,
    ) -> TransitionResult:
        life = context.lifecycle
        assert life is not None
        now = context.now or signal.observed_at
        market = context.snapshot.market

        # Precedence: KILL → MARKET_CLOSE → MAX_HOLD → REVERSAL → FLAT
        if life.state in {LifecycleState.ACTIVE, LifecycleState.EXIT_PENDING}:
            inst = life.instrument_id
            if inst is None:
                return TransitionResult(decision=decision, suppressed=True, suppress_reason="NO_INSTRUMENT")

            if context.kill_switch_active and life.state is LifecycleState.ACTIVE:
                intent = self._flatten(signal, context, inst, "KILL_SWITCH")
                self._last_direction = signal.direction
                return TransitionResult(decision=decision, intents=[intent])

            if (
                market.event_end is not None
                and context.flatten_before_close is not None
                and life.state is LifecycleState.ACTIVE
                and now >= market.event_end - context.flatten_before_close
            ):
                intent = self._flatten(signal, context, inst, "MARKET_CLOSE_BOUNDARY")
                self._last_direction = signal.direction
                return TransitionResult(decision=decision, intents=[intent])

            if (
                life.activated_at is not None
                and context.max_hold is not None
                and life.state is LifecycleState.ACTIVE
                and now - life.activated_at >= context.max_hold
            ):
                intent = self._exit(signal, context, inst, "MAX_HOLD")
                self._last_direction = signal.direction
                return TransitionResult(decision=decision, intents=[intent])

            if life.state is LifecycleState.ACTIVE:
                # Reversal vs entry direction inferred from position instrument
                entry_was_up = inst == market.yes.instrument_id
                if signal.direction is Direction.DOWN and entry_was_up:
                    intent = self._exit(signal, context, inst, "SIGNAL_REVERSAL")
                    self._last_direction = signal.direction
                    return TransitionResult(decision=decision, intents=[intent])
                if signal.direction is Direction.UP and not entry_was_up:
                    intent = self._exit(signal, context, inst, "SIGNAL_REVERSAL")
                    self._last_direction = signal.direction
                    return TransitionResult(decision=decision, intents=[intent])
                if context.exit_on_flat and signal.direction is Direction.FLAT:
                    intent = self._exit(signal, context, inst, "SIGNAL_FLAT")
                    self._last_direction = signal.direction
                    return TransitionResult(decision=decision, intents=[intent])

            self._last_direction = signal.direction
            return TransitionResult(
                decision=decision,
                suppressed=True,
                suppress_reason="ACTIVE_NO_EXIT",
            )

        if life.state is LifecycleState.ENTRY_PENDING:
            self._last_direction = signal.direction
            return TransitionResult(
                decision=decision,
                suppressed=True,
                suppress_reason="ENTRY_PENDING",
            )

        if life.state is LifecycleState.TERMINAL:
            self._last_direction = signal.direction
            return TransitionResult(
                decision=decision,
                suppressed=True,
                suppress_reason="TERMINAL",
            )

        # FLAT — entry eligibility
        if signal.direction in (Direction.UP, Direction.DOWN):
            intent = self._build_enter_intent(signal, context, decision)
            self._last_direction = signal.direction
            self._last_intent_id = intent.intent_id.value
            self._last_intent_at = signal.observed_at
            return TransitionResult(decision=decision, intents=[intent])

        self._last_direction = signal.direction
        return TransitionResult(
            decision=decision,
            suppressed=True,
            suppress_reason="NO_ENTRY_TRANSITION",
        )

    def _apply_signal_transition(
        self,
        signal: DirectionalSignal,
        context: DecisionContext,
        decision: ObserveDecision,
    ) -> TransitionResult:
        """R4 dry-plan transition (no portfolio)."""
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

    def _exit(
        self,
        signal: DirectionalSignal,
        context: DecisionContext,
        instrument_id,
        reason: str,
    ) -> ExitIntent:
        return ExitIntent(
            intent_id=new_intent_id(),
            strategy_id=self.STRATEGY_ID,
            instrument_id=instrument_id,
            market_id=context.snapshot.market.market_id,
            created_at=signal.observed_at,
            correlation_id=signal.correlation_id,
            causation_id=signal.causation_id,
            reason_code=reason,
            evidence={"signal_direction": signal.direction.value},
        )

    def _flatten(
        self,
        signal: DirectionalSignal,
        context: DecisionContext,
        instrument_id,
        reason: str,
    ) -> FlattenIntent:
        return FlattenIntent(
            intent_id=new_intent_id(),
            strategy_id=self.STRATEGY_ID,
            instrument_id=instrument_id,
            market_id=context.snapshot.market.market_id,
            created_at=signal.observed_at,
            correlation_id=signal.correlation_id,
            causation_id=signal.causation_id,
            reason_code=reason,
            evidence={"signal_direction": signal.direction.value},
        )
