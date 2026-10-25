"""A strategy that lives only in the tests: proves a new kind plugs in with no framework edit.

It reads books (the ask70 input contract), never trades, and records the best asks it saw.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from tyrex_pm.core.ids import StrategyId
from tyrex_pm.core.time_authority import ClockTimeAuthority, TimeAuthority
from tyrex_pm.facts.contract import EligibilityFacts
from tyrex_pm.facts.mappings import ASK70_INPUT_CONTRACT
from tyrex_pm.strategies.base import BaseDriver
from tyrex_pm.strategies.decisions import StrategyAction, StrategyDecision
from tyrex_pm.strategies.evaluation import StrategyEvaluation
from tyrex_pm.strategies.registry import StrategyPlugin
from tyrex_pm.strategies.research import ResearchSchema

KIND = "v1_watcher"


@dataclass(frozen=True)
class WatcherConfig:
    tau_min_s: float = 30.0
    tau_max_s: float = 290.0
    max_clock_uncertainty_ms: float = 750.0


@dataclass
class WatcherDriver(BaseDriver):
    config: WatcherConfig
    time_authority: TimeAuthority
    target_notional: Decimal = Decimal("5")
    window_id: str = "unbound"
    fee_curve: Any = None
    seen: list = field(default_factory=list)

    @property
    def strategy_id(self) -> StrategyId:
        return StrategyId(KIND)

    def evaluate(self, *, market_snapshot, causation_id, correlation_id, context, trigger,
                 features) -> StrategyEvaluation:
        del features
        ask = market_snapshot.yes_quote.best_ask
        self.seen.append((str(trigger), None if ask is None else str(ask)))
        decision = StrategyDecision(
            action=StrategyAction.WAIT,
            reason_code="WATCH_ONLY",
            decided_at=context.now or market_snapshot.observed_at,
            correlation_id=correlation_id,
            causation_id=causation_id,
            evidence={"up_ask": None if ask is None else str(ask), "tau_s": context.tau_s},
        )
        return StrategyEvaluation(
            decision=decision, intents=(), strategy_id=None,
            eligibility=EligibilityFacts(strategy_inputs_eligible=True, model_ready=True),
        )


def _load(parameters: Mapping[str, Any], file: str) -> WatcherConfig:
    unknown = set(parameters) - {"tau_min_s", "tau_max_s", "max_clock_uncertainty_ms"}
    if unknown:
        raise ValueError(f"{file}: unknown watcher fields {sorted(unknown)}")
    return WatcherConfig(**{k: float(v) for k, v in parameters.items()})


def _create(*, config: WatcherConfig, time_authority=None, clock=None,
            target_notional: Decimal = Decimal("5"), window_id: str = "unbound",
            fee_curve: Any = None) -> WatcherDriver:
    authority = time_authority or ClockTimeAuthority(clock=clock)
    return WatcherDriver(config=config, time_authority=authority,
                         target_notional=target_notional, window_id=window_id,
                         fee_curve=fee_curve)


WATCHER_PLUGIN = StrategyPlugin(
    kind=KIND,
    input_contract=ASK70_INPUT_CONTRACT,
    load_config=_load,
    create_driver=_create,
    entry_tau_bounds=lambda c: (c.tau_min_s, c.tau_max_s),
    max_clock_uncertainty_ms=lambda c: c.max_clock_uncertainty_ms,
    research=ResearchSchema(label="Watcher (test only)", essentials=("tau_min_s",)),
)
