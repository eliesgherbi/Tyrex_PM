"""feature_probe driver — FeatureBundle + books; no settlement kwargs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tyrex_pm.core.ids import CorrelationId, EventId, StrategyId
from tyrex_pm.core.time_authority import ClockTimeAuthority, FakeTimeAuthority, TimeAuthority
from tyrex_pm.facts.contract import EligibilityFacts
from tyrex_pm.indicators.graph import FeatureBundle
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.strategies.context import DecisionContext, StrategyContext
from tyrex_pm.strategies.evaluation import StrategyEvaluation
from tyrex_pm.strategies.feature_probe.config import FeatureProbeConfig
from tyrex_pm.strategies.feature_probe.strategy import FeatureProbeStrategy


@dataclass
class FeatureProbeDriver:
    strategy: FeatureProbeStrategy
    time_authority: TimeAuthority
    target_notional: Decimal
    window_id: str
    config: FeatureProbeConfig
    fee_curve: Any = None

    @property
    def strategy_id(self) -> StrategyId:
        return FeatureProbeStrategy.STRATEGY_ID

    def on_start(self, context: StrategyContext) -> None:
        self.strategy.on_start(context)

    def on_stop(self, reason: str) -> None:
        self.strategy.on_stop(reason)

    def persistence_slice(self) -> dict[str, Any]:
        return self.strategy.persistence_slice()

    def restore_persistence_slice(self, data: dict[str, Any]) -> None:
        self.strategy.restore_state(
            decision_epoch=int(data.get("strategy_epoch") or 0),
            entry_lineage_consumed=bool(data.get("entry_lineage_consumed", False)),
            window_id=data.get("window_id"),
        )

    def evaluate(
        self,
        *,
        market_snapshot: DecisionSnapshot,
        causation_id: EventId | None,
        correlation_id: CorrelationId,
        context: DecisionContext,
        trigger: str,
        features: FeatureBundle | None = None,
    ) -> StrategyEvaluation:
        del causation_id, correlation_id, trigger
        bundle = features or FeatureBundle()
        decision, intents = self.strategy.on_decision(
            market_snapshot=market_snapshot,
            context=context,
            features=bundle,
        )
        eligible = bool(decision.evidence.get("strategy_inputs_eligible", False))
        raw_blockers = decision.evidence.get("blockers", ())
        if not isinstance(raw_blockers, (list, tuple)):
            raw_blockers = ()
        return StrategyEvaluation(
            decision=decision,
            intents=tuple(intents),
            strategy_id=self.strategy_id,
            eligibility=EligibilityFacts(
                strategy_inputs_eligible=eligible,
                model_ready=eligible,
                blockers=tuple(str(value) for value in raw_blockers),
            ),
            reporting_context={"decision_input": None, "calibration": {}},
        )


def create_feature_probe_driver(
    *,
    config: FeatureProbeConfig,
    time_authority: TimeAuthority | None = None,
    clock=None,
    target_notional: Decimal = Decimal("5"),
    window_id: str = "unbound",
    fee_curve=None,  # noqa: ANN001 — accepted for uniform factory kwargs
) -> FeatureProbeDriver:
    del fee_curve
    if time_authority is None:
        if clock is None:
            raise ValueError("feature_probe driver requires a time authority or clock")
        from tyrex_pm.core.clock import FakeClock

        time_authority = (
            FakeTimeAuthority(clock=clock)
            if isinstance(clock, FakeClock)
            else ClockTimeAuthority(clock=clock)
        )
    return FeatureProbeDriver(
        strategy=FeatureProbeStrategy(config=config),
        time_authority=time_authority,
        target_notional=target_notional,
        window_id=window_id,
        config=config,
    )
