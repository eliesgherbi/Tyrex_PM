"""Strategy evaluation context (immutable views only)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.core.ids import RunId, StrategyId
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot


@dataclass(frozen=True, kw_only=True)
class StrategyContext:
    run_id: RunId
    strategy_id: StrategyId
    mode: RuntimeMode
    market: BinaryMarket


@dataclass(frozen=True, kw_only=True)
class DecisionContext:
    """Immutable context for on_signal — no mutable stores."""

    run_id: RunId
    mode: RuntimeMode
    snapshot: DecisionSnapshot
    target_notional: Decimal
    max_price: Decimal | None = None
