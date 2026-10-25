"""feature_probe strategy — observe L1 imbalance; enter only when emit_entry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from tyrex_pm.core.ids import StrategyId
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.intents import EnterIntent, LiquidityRole, new_intent_id
from tyrex_pm.indicators.graph import FeatureBundle
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.strategies.common import capped_entry_price, outcome_instrument
from tyrex_pm.strategies.context import DecisionContext, StrategyContext
from tyrex_pm.strategies.decisions import IntentLike, StrategyAction, StrategyDecision
from tyrex_pm.strategies.feature_probe.config import FeatureProbeConfig
from tyrex_pm.strategies.feature_probe.reasons import FeatureProbeReason


def _imbalance_from_bundle(features: FeatureBundle) -> tuple[str | None, Any]:
    for key, value in features.values.items():
        if key.startswith("imbalance|"):
            return key, value
    return None, None


@dataclass
class FeatureProbeStrategy:
    STRATEGY_ID = StrategyId("feature_probe")

    config: FeatureProbeConfig
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
        features: FeatureBundle,
    ) -> tuple[StrategyDecision, list[IntentLike]]:
        now = context.now if context.now is not None else market_snapshot.observed_at
        if now.tzinfo is None:
            now = datetime.now(timezone.utc)
        up_ask = market_snapshot.yes_quote.best_ask
        down_ask = market_snapshot.no_quote.best_ask
        books_ok = up_ask is not None and down_ask is not None
        feature_key, imbalance = _imbalance_from_bundle(features)
        feature_ready = feature_key is not None and imbalance is not None
        blockers: list[str] = []
        if not feature_ready:
            blockers.append(FeatureProbeReason.FEATURE_NOT_READY.value)
        if not books_ok:
            blockers.append("BOOK_ASK_MISSING")
        eligible = not blockers
        evidence = {
            "up_ask": None if up_ask is None else str(up_ask),
            "down_ask": None if down_ask is None else str(down_ask),
            "imbalance_key": feature_key,
            "imbalance": None if imbalance is None else str(imbalance),
            "emit_entry": self.config.emit_entry,
            "strategy_inputs_eligible": eligible,
            "blockers": list(blockers),
        }

        if context.position_quantity > 0:
            return self._decision(
                StrategyAction.HOLD,
                FeatureProbeReason.HOLD_POSITION,
                now,
                market_snapshot,
                evidence,
            )

        if not feature_ready:
            return self._decision(
                StrategyAction.SKIP,
                FeatureProbeReason.FEATURE_NOT_READY,
                now,
                market_snapshot,
                evidence,
            )

        if not books_ok:
            return self._decision(
                StrategyAction.SKIP,
                FeatureProbeReason.SKIP_NO_BOOK,
                now,
                market_snapshot,
                evidence,
            )

        if not self.config.emit_entry:
            return self._decision(
                StrategyAction.SKIP,
                FeatureProbeReason.FEATURE_OBSERVED,
                now,
                market_snapshot,
                evidence,
            )

        if not context.entry_allowed:
            return self._decision(
                StrategyAction.BLOCKED,
                FeatureProbeReason.BLOCKED,
                now,
                market_snapshot,
                {**evidence, "entry_block_reason": context.entry_block_reason},
            )

        if self._entry_lineage_consumed:
            return self._decision(
                StrategyAction.WAIT,
                FeatureProbeReason.WAIT_NO_ASK_HIT,
                now,
                market_snapshot,
                {**evidence, "entry_lineage_consumed": True},
            )

        selected = self._select_leg(up_ask=up_ask, down_ask=down_ask)
        if selected is None:
            return self._decision(
                StrategyAction.WAIT,
                FeatureProbeReason.WAIT_NO_ASK_HIT,
                now,
                market_snapshot,
                evidence,
            )

        outcome, ask = selected
        instrument = outcome_instrument(market_snapshot.market, outcome)
        max_price = capped_entry_price(ask, self.config.max_price_pad)
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
            reason_code=FeatureProbeReason.ENTRY_ASK_HIT.value,
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
            liquidity_role=LiquidityRole.TAKER,
        )
        decision, _ = self._decision(
            StrategyAction.ENTER,
            FeatureProbeReason.ENTRY_ASK_HIT,
            now,
            market_snapshot,
            dict(intent.evidence),
        )
        return decision, [intent]

    def _select_leg(
        self, *, up_ask: Decimal, down_ask: Decimal
    ) -> tuple[OutcomeSide, Decimal] | None:
        threshold = self.config.entry_ask_threshold
        if up_ask >= threshold:
            return OutcomeSide.YES, up_ask
        if down_ask >= threshold:
            return OutcomeSide.NO, down_ask
        return None

    def _decision(
        self,
        action: StrategyAction,
        reason: FeatureProbeReason,
        now: datetime,
        market_snapshot: DecisionSnapshot,
        evidence: dict[str, Any],
    ) -> tuple[StrategyDecision, list[IntentLike]]:
        return (
            StrategyDecision(
                action=action,
                reason_code=reason.value,
                decided_at=now,
                correlation_id=market_snapshot.correlation_id,
                causation_id=market_snapshot.causation_id,
                evidence=evidence,
                strategy_id=self.STRATEGY_ID,
            ),
            [],
        )
