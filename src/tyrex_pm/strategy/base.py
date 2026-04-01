"""Composable strategy base (milestone v1.03 stub; extended in v1.05+)."""

from __future__ import annotations

from typing import Any

from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

from tyrex_pm.strategy.logutil import strategy_started_line


class BaseComposableStrategyConfig(StrategyConfig, frozen=True, kw_only=True):
    """Minimal config; add fields in later milestones."""


class BaseComposableStrategy(Strategy):
    """
    Policy-composable root for v1 copy and future strategies.
    Logs a single structured line on start for observability baseline (v1.03).
    """

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

    def on_start(self) -> None:
        self.log.info(
            strategy_started_line(
                trader_id=str(self.trader_id),
                strategy_id=str(self.id),
            )
        )

    def on_event(self, event: Any) -> None:  # noqa: ANN401 — Nautilus event union
        """Stub: v1.05+ routes guru/custom events here."""
