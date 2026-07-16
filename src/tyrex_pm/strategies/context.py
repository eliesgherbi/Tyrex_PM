"""Strategy evaluation context (immutable views only)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from tyrex_pm.core.ids import RunId, StrategyId
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.lifecycle.trade_lifecycle import LifecycleSnapshot
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
    lifecycle: LifecycleSnapshot | None = None
    position_quantity: Decimal = Decimal("0")
    now: datetime | None = None
    max_hold: timedelta | None = None
    flatten_before_close: timedelta | None = None
    exit_on_flat: bool = True
    kill_switch_active: bool = False
