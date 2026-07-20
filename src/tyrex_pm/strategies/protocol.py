"""Minimal strategy protocol — neutral decisions and IntentLike outputs."""

from __future__ import annotations

from typing import Protocol

from tyrex_pm.signals.directional import DirectionalSignal
from tyrex_pm.strategies.context import DecisionContext, StrategyContext
from tyrex_pm.strategies.decisions import IntentLike, StrategyDecision


class StopReason(str):
    """Opaque stop reason string (NORMAL, ERROR, ...)."""


class Strategy(Protocol):
    def on_start(self, context: StrategyContext) -> None: ...

    def on_signal(
        self,
        signal: DirectionalSignal,
        context: DecisionContext,
    ) -> tuple[StrategyDecision, list[IntentLike]]: ...

    def on_stop(self, reason: str) -> None: ...
