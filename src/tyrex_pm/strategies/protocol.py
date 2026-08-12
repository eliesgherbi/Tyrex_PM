"""Production strategy driver protocol — evaluate(snapshot) not on_signal."""

from __future__ import annotations

from typing import Any, Protocol

from tyrex_pm.core.ids import CorrelationId, EventId, StrategyId
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.strategies.context import DecisionContext, StrategyContext
from tyrex_pm.strategies.evaluation import StrategyEvaluation


class StrategyDriver(Protocol):
    """Per-window strategy adapter used by the market session runtime."""

    strategy_id: StrategyId
    target_notional: Any
    time_authority: Any
    config: Any

    def on_start(self, context: StrategyContext) -> None: ...

    def on_stop(self, reason: str) -> None: ...

    def evaluate(
        self,
        *,
        market_snapshot: DecisionSnapshot,
        causation_id: EventId | None,
        correlation_id: CorrelationId,
        context: DecisionContext,
        trigger: str,
        settlement_reference: Any,
        settlement_reference_fresh: bool,
    ) -> StrategyEvaluation: ...

    def persistence_slice(self) -> dict[str, Any]: ...

    def restore_persistence_slice(self, data: dict[str, Any]) -> None: ...
