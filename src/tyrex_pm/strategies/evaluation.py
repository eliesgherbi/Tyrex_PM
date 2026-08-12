"""Neutral strategy evaluation result (framework-generic, not z_gap-owned)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tyrex_pm.core.ids import StrategyId
from tyrex_pm.facts.contract import EligibilityFacts
from tyrex_pm.strategies.decisions import IntentLike, StrategyDecision


@dataclass(frozen=True)
class StrategyEvaluation:
    decision: StrategyDecision
    intents: tuple[IntentLike, ...]
    strategy_id: StrategyId
    eligibility: EligibilityFacts
    reporting_context: dict[str, Any] = field(default_factory=dict)
