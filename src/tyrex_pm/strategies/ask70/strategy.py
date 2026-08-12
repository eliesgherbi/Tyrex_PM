"""ask70 harness strategy — enter first ask >= threshold; no strategy exits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.ids import StrategyId
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.intents import EnterIntent, new_intent_id
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.strategies.ask70.config import Ask70Config
from tyrex_pm.strategies.ask70.reasons import Ask70Reason
from tyrex_pm.strategies.context import DecisionContext, StrategyContext
from tyrex_pm.strategies.decisions import IntentLike, StrategyAction, StrategyDecision


@dataclass
class Ask70Strategy:
    STRATEGY_ID = StrategyId("ask70")

    config: Ask70Config
    _decision_epoch_counter: int = 0
    _entry_lineage_consumed: bool = False
    _window_id: str | None = None

    def on_start(self, context: StrategyContext) -> None:
        self._window_id = context.market.market_id.value

    def on_stop(self, reason: str) -> None:
        del reason

    def persistence_slice(self) -> dict:
        return {
            "strategy_epoch": self._decision_epoch_counter,
            "entry_lineage_consumed": self._entry_lineage_consumed,
            "window_id": self._window_id,
        }

    def restore_state(
        self,
        *,
        decision_epoch: int = 0,
        entry_lineage_consumed: bool = False,
        window_id: str | None = None,
    ) -> None:
        self._decision_epoch_counter = decision_epoch
        self._entry_lineage_consumed = entry_lineage_consumed
        self._window_id = window_id

    def on_decision(
        self,
        *,
        market_snapshot: DecisionSnapshot,
        context: DecisionContext,
    ) -> tuple[StrategyDecision, list[IntentLike]]:
        now = context.now if context.now.tzinfo else datetime.now(timezone.utc)
        up_ask = market_snapshot.yes_quote.best_ask
        down_ask = market_snapshot.no_quote.best_ask
        books_ok = up_ask is not None and down_ask is not None
        evidence = {
            "up_ask": None if up_ask is None else str(up_ask),
            "down_ask": None if down_ask is None else str(down_ask),
            "entry_ask_threshold": str(self.config.entry_ask_threshold),
            "leg_preference": self.config.leg_preference,
            "strategy_inputs_eligible": books_ok,
            "blockers": [] if books_ok else ["BOOK_ASK_MISSING"],
        }

        if context.position_quantity > 0:
            return (
                StrategyDecision(
                    action=StrategyAction.HOLD,
                    reason_code=Ask70Reason.HOLD_POSITION.value,
                    decided_at=now,
                    correlation_id=market_snapshot.correlation_id,
                    causation_id=market_snapshot.causation_id,
                    evidence=evidence,
                    strategy_id=self.STRATEGY_ID,
                ),
                [],
            )

        if not books_ok:
            return (
                StrategyDecision(
                    action=StrategyAction.SKIP,
                    reason_code=Ask70Reason.SKIP_NO_BOOK.value,
                    decided_at=now,
                    correlation_id=market_snapshot.correlation_id,
                    causation_id=market_snapshot.causation_id,
                    evidence=evidence,
                    strategy_id=self.STRATEGY_ID,
                ),
                [],
            )

        if not context.entry_allowed:
            return (
                StrategyDecision(
                    action=StrategyAction.BLOCKED,
                    reason_code=Ask70Reason.BLOCKED.value,
                    decided_at=now,
                    correlation_id=market_snapshot.correlation_id,
                    causation_id=market_snapshot.causation_id,
                    evidence={**evidence, "entry_block_reason": context.entry_block_reason},
                    strategy_id=self.STRATEGY_ID,
                ),
                [],
            )

        if self._entry_lineage_consumed:
            return (
                StrategyDecision(
                    action=StrategyAction.WAIT,
                    reason_code=Ask70Reason.WAIT_NO_ASK_HIT.value,
                    decided_at=now,
                    correlation_id=market_snapshot.correlation_id,
                    causation_id=market_snapshot.causation_id,
                    evidence={**evidence, "entry_lineage_consumed": True},
                    strategy_id=self.STRATEGY_ID,
                ),
                [],
            )

        selected = self._select_leg(up_ask=up_ask, down_ask=down_ask)
        if selected is None:
            return (
                StrategyDecision(
                    action=StrategyAction.WAIT,
                    reason_code=Ask70Reason.WAIT_NO_ASK_HIT.value,
                    decided_at=now,
                    correlation_id=market_snapshot.correlation_id,
                    causation_id=market_snapshot.causation_id,
                    evidence=evidence,
                    strategy_id=self.STRATEGY_ID,
                ),
                [],
            )

        outcome, ask = selected
        instrument = (
            market_snapshot.market.yes if outcome is OutcomeSide.YES else market_snapshot.market.no
        )
        max_price = ask + self.config.max_price_pad
        if max_price > Decimal("0.99"):
            max_price = Decimal("0.99")
        self._decision_epoch_counter += 1
        self._entry_lineage_consumed = True
        intent = EnterIntent(
            intent_id=new_intent_id(),
            strategy_id=self.STRATEGY_ID,
            instrument_id=instrument.instrument_id,
            market_id=market_snapshot.market.market_id,
            created_at=now,
            correlation_id=market_snapshot.correlation_id,
            causation_id=market_snapshot.causation_id,
            reason_code=Ask70Reason.ENTRY_ASK_HIT.value,
            evidence={
                **evidence,
                "selected_outcome": outcome.value,
                "selected_ask": str(ask),
                "max_price": str(max_price),
            },
            target_notional=context.target_notional,
            outcome=outcome,
            decision_epoch=self._decision_epoch_counter,
            max_price=max_price,
        )
        return (
            StrategyDecision(
                action=StrategyAction.ENTER,
                reason_code=Ask70Reason.ENTRY_ASK_HIT.value,
                decided_at=now,
                correlation_id=market_snapshot.correlation_id,
                causation_id=market_snapshot.causation_id,
                evidence=dict(intent.evidence),
                strategy_id=self.STRATEGY_ID,
            ),
            [intent],
        )

    def _select_leg(
        self, *, up_ask: Decimal, down_ask: Decimal
    ) -> tuple[OutcomeSide, Decimal] | None:
        threshold = self.config.entry_ask_threshold
        up_hit = up_ask >= threshold
        down_hit = down_ask >= threshold
        preference = self.config.leg_preference
        if preference == "up_then_down":
            order = ((OutcomeSide.YES, up_ask, up_hit), (OutcomeSide.NO, down_ask, down_hit))
        elif preference == "down_then_up":
            order = ((OutcomeSide.NO, down_ask, down_hit), (OutcomeSide.YES, up_ask, up_hit))
        else:
            # first_hit: deterministic scan UP then DOWN at equal priority by ask order
            order = ((OutcomeSide.YES, up_ask, up_hit), (OutcomeSide.NO, down_ask, down_hit))
        for outcome, ask, hit in order:
            if hit:
                return outcome, ask
        return None
