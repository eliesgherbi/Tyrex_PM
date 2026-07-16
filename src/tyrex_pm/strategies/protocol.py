"""Minimal strategy protocol with R4 consumers only."""

from __future__ import annotations

from typing import Protocol

from tyrex_pm.core.intents import EnterIntent
from tyrex_pm.signals.directional import DirectionalSignal
from tyrex_pm.strategies.context import DecisionContext, StrategyContext
from tyrex_pm.strategies.framework_validation.reference_momentum import ObserveDecision


class StopReason(str):
    """Opaque stop reason string (NORMAL, ERROR, ...)."""


class Strategy(Protocol):
    def on_start(self, context: StrategyContext) -> None: ...

    def on_signal(
        self,
        signal: DirectionalSignal,
        context: DecisionContext,
    ) -> tuple[ObserveDecision, list[EnterIntent]]: ...

    def on_stop(self, reason: str) -> None: ...
