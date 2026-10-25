"""The strategy driver contract every host relies on (live, paper, observation, replay).

A driver is created per window by its plugin (``registry.StrategyPlugin.create_driver``).
Optional feedback methods have defaults in ``strategies.base.BaseDriver``; hosts call them
directly and never probe a driver with ``getattr``.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from tyrex_pm.core.ids import CorrelationId, EventId, StrategyId
from tyrex_pm.core.time_authority import TimeAuthority
from tyrex_pm.domain.polymarket.ptb import PtbSnapshot
from tyrex_pm.indicators.graph import FeatureBundle
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.strategies.base import EpisodeView
from tyrex_pm.strategies.context import DecisionContext, StrategyContext
from tyrex_pm.strategies.evaluation import StrategyEvaluation
from tyrex_pm.strategies.outcomes import EntryOutcome, ExitOutcome
from tyrex_pm.strategies.triggers import EvaluationTrigger


@runtime_checkable
class StrategyDriver(Protocol):
    """Per-window strategy adapter used by every host."""

    # Stable identity of the strategy in contexts, intents and records.
    strategy_id: StrategyId
    config: Any
    time_authority: TimeAuthority
    target_notional: Decimal
    window_id: str
    # Strategy fee estimate passed to the host's fee checks; None when the strategy has none.
    fee_curve: Any

    def on_start(self, context: StrategyContext) -> None: ...

    def on_stop(self, reason: str) -> None: ...

    def evaluate(
        self,
        *,
        market_snapshot: DecisionSnapshot,
        causation_id: EventId | None,
        correlation_id: CorrelationId,
        context: DecisionContext,
        trigger: EvaluationTrigger,
        features: FeatureBundle,
    ) -> StrategyEvaluation: ...

    def configure_ptb(self, ptb: PtbSnapshot) -> None: ...

    def on_entry_outcome(self, outcome: EntryOutcome) -> None: ...

    def on_exit_outcome(self, outcome: ExitOutcome) -> None: ...

    def on_hold_committed(self) -> None: ...

    def restore_window_usage(self, **usage: Any) -> None: ...

    def value_exit_refusal(
        self, snapshot: Any, context: Any, features: Any, token_id: str, minimum_price: Any
    ) -> str | None: ...

    def episode_view(self) -> EpisodeView | None: ...

    def persistence_slice(self) -> dict[str, Any]: ...

    def restore_persistence_slice(self, data: Mapping[str, Any]) -> None: ...
