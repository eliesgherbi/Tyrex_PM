"""ReferenceMomentumStrategy — R3 observe + R4/R5 transitions.

Framework-validation only (not an alpha strategy).

Uses neutral ``StrategyDecision`` / ``IntentLike`` (F1). Validation-specific
labels such as WOULD_ENTER_UP/DOWN are retained in ``evidence["validation_kind"]``
for fact/report continuity — they are not generic protocol actions.

When ``DecisionContext.lifecycle`` is None (R4 dry), signal-transition rules apply.
When lifecycle is provided (R5), eligibility uses authoritative lifecycle/position.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from tyrex_pm.core.ids import StrategyId
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.intents import EnterIntent, ExitIntent, FlattenIntent, new_intent_id
from tyrex_pm.lifecycle.trade_lifecycle import LifecycleState
from tyrex_pm.signals.directional import Direction, DirectionalSignal
from tyrex_pm.strategies.context import DecisionContext, StrategyContext
from tyrex_pm.strategies.decisions import IntentLike, StrategyAction, StrategyDecision


class ObserveDecisionKind(str, Enum):
    """Validation-strategy labels stored in evidence (not F1 generic actions)."""

    WOULD_ENTER_UP = "WOULD_ENTER_UP"
    WOULD_ENTER_DOWN = "WOULD_ENTER_DOWN"
    HOLD = "HOLD"
    SKIP = "SKIP"


@dataclass
class TransitionResult:
    decision: StrategyDecision
    intents: list[IntentLike] = field(default_factory=list)
    suppressed: bool = False
    suppress_reason: str | None = None


def _validation_kind_to_action(kind: ObserveDecisionKind) -> StrategyAction:
    if kind is ObserveDecisionKind.SKIP:
        return StrategyAction.SKIP
    if kind is ObserveDecisionKind.HOLD:
        return StrategyAction.HOLD
    if kind in (ObserveDecisionKind.WOULD_ENTER_UP, ObserveDecisionKind.WOULD_ENTER_DOWN):
        return StrategyAction.ENTER
    raise ValueError(f"unmapped ObserveDecisionKind: {kind!r}")


def _make_decision(
    *,
    kind: ObserveDecisionKind,
    reason_code: str,
    observed_at: datetime,
    correlation_id,
    causation_id,
    signal_direction: Direction,
    evidence: dict[str, Any],
    strategy_id: StrategyId,
) -> StrategyDecision:
    return StrategyDecision(
        action=_validation_kind_to_action(kind),
        reason_code=reason_code,
        decided_at=observed_at,
        correlation_id=correlation_id,
        causation_id=causation_id,
        strategy_id=strategy_id,
        evidence={
            **evidence,
            "validation_kind": kind.value,
            "signal_direction": signal_direction.value,
        },
    )


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

    def evaluate(self, signal: DirectionalSignal) -> StrategyDecision:
        if signal.direction is Direction.UNAVAILABLE:
            return _make_decision(
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
            return _make_decision(
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
            return _make_decision(
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
        return _make_decision(
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
    ) -> tuple[StrategyDecision, list[IntentLike]]:
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
        decision: StrategyDecision,
    ) -> TransitionResult:
        life = context.lifecycle
        assert life is not None
        now = context.now or signal.observed_at
        market = context.snapshot.market

        busy_exit = {
            LifecycleState.EXIT_REQUESTED,
            LifecycleState.EXIT_PENDING,
            LifecycleState.EXIT_RETRY_WAIT,
        }

        if life.state is LifecycleState.MANUAL_INTERVENTION:
            self._last_direction = signal.direction
            return TransitionResult(
                decision=decision,
                suppressed=True,
                suppress_reason="MANUAL_INTERVENTION",
            )

        if life.state in busy_exit or life.state is LifecycleState.ACTIVE:
            inst = life.instrument_id
            if inst is None:
                return TransitionResult(
                    decision=decision, suppressed=True, suppress_reason="NO_INSTRUMENT"
                )

            # Precedence: KILL → MARKET_CLOSE → MAX_HOLD → REVERSAL → FLAT
            want_flatten = False
            want_exit = False
            reason = "ACTIVE_NO_EXIT"
            escalate = False

            if context.kill_switch_active:
                want_flatten = True
                reason = "KILL_SWITCH"
                escalate = True
            elif (
                market.event_end is not None
                and context.flatten_before_close is not None
                and now >= market.event_end - context.flatten_before_close
            ):
                want_flatten = True
                reason = "MARKET_CLOSE_BOUNDARY"
                escalate = True
            elif life.state is LifecycleState.ACTIVE:
                if (
                    life.activated_at is not None
                    and context.max_hold is not None
                    and now - life.activated_at >= context.max_hold
                ):
                    want_exit = True
                    reason = "MAX_HOLD"
                else:
                    entry_was_up = inst == market.yes.instrument_id
                    if signal.direction is Direction.DOWN and entry_was_up:
                        want_exit = True
                        reason = "SIGNAL_REVERSAL"
                    elif signal.direction is Direction.UP and not entry_was_up:
                        want_exit = True
                        reason = "SIGNAL_REVERSAL"
                    elif context.exit_on_flat and signal.direction is Direction.FLAT:
                        want_exit = True
                        reason = "SIGNAL_FLAT"

            if life.state in busy_exit and not escalate and not context.exit_escalate:
                # One outstanding exit — suppress duplicates unless escalated.
                self._last_direction = signal.direction
                return TransitionResult(
                    decision=decision,
                    suppressed=True,
                    suppress_reason="EXIT_ALREADY_OUTSTANDING",
                )

            if (want_flatten or want_exit or context.exit_escalate) and (
                context.exit_allowed or escalate or context.exit_escalate
            ):
                if want_flatten or (
                    context.exit_urgency == "URGENT" or escalate or context.exit_escalate
                ):
                    intent = self._flatten(
                        signal,
                        context,
                        inst,
                        reason if want_flatten else (context.exit_block_reason or reason),
                    )
                else:
                    intent = self._exit(signal, context, inst, reason)
                self._last_direction = signal.direction
                return TransitionResult(decision=decision, intents=[intent])

            self._last_direction = signal.direction
            return TransitionResult(
                decision=decision,
                suppressed=True,
                suppress_reason=context.exit_block_reason or reason,
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

        # FLAT — entry eligibility gated by host retry controller.
        if signal.direction in (Direction.UP, Direction.DOWN):
            if not context.entry_allowed:
                self._last_direction = signal.direction
                return TransitionResult(
                    decision=decision,
                    suppressed=True,
                    suppress_reason=context.entry_block_reason or "ENTRY_RETRY_WAIT",
                )
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
        decision: StrategyDecision,
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
        decision: StrategyDecision,
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
                "validation_kind": decision.evidence.get("validation_kind"),
                "strategy_action": decision.action.value,
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
