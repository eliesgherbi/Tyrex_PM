"""ask70 driver — book-only entry harness for protection validation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tyrex_pm.core.ids import CorrelationId, EventId, StrategyId
from tyrex_pm.core.time_authority import ClockTimeAuthority, FakeTimeAuthority, TimeAuthority
from tyrex_pm.facts.contract import EligibilityFacts
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.strategies.ask70.config import Ask70Config
from tyrex_pm.strategies.ask70.strategy import Ask70Strategy
from tyrex_pm.strategies.context import DecisionContext, StrategyContext
from tyrex_pm.strategies.evaluation import StrategyEvaluation


@dataclass
class Ask70Driver:
    strategy: Ask70Strategy
    time_authority: TimeAuthority
    target_notional: Decimal
    window_id: str
    config: Ask70Config
    # Present so market_runtime open_session can share a uniform driver surface.
    fee_curve: Any = None

    @property
    def strategy_id(self) -> StrategyId:
        return Ask70Strategy.STRATEGY_ID

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
        settlement_reference: Decimal | None,
        settlement_reference_fresh: bool,
    ) -> StrategyEvaluation:
        del causation_id, correlation_id, trigger, settlement_reference, settlement_reference_fresh
        decision, intents = self.strategy.on_decision(
            market_snapshot=market_snapshot,
            context=context,
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


def create_ask70_driver(
    *,
    config: Ask70Config,
    time_authority: TimeAuthority | None = None,
    clock=None,
    target_notional: Decimal = Decimal("5"),
    window_id: str = "unbound",
    fee_curve=None,  # noqa: ANN001 — accepted for uniform factory kwargs
) -> Ask70Driver:
    del fee_curve
    if time_authority is None:
        if clock is None:
            raise ValueError("ask70 driver requires a time authority or clock")
        from tyrex_pm.core.clock import FakeClock

        time_authority = (
            FakeTimeAuthority(clock=clock)
            if isinstance(clock, FakeClock)
            else ClockTimeAuthority(clock=clock)
        )
    return Ask70Driver(
        strategy=Ask70Strategy(config=config),
        time_authority=time_authority,
        target_notional=target_notional,
        window_id=window_id,
        config=config,
    )
