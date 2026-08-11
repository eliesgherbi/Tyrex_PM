"""Strategy evaluation context (immutable views only)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

from tyrex_pm.core.ids import InstrumentId, RunId, StrategyId
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.domain.polymarket.resolution_capability import (
    DISABLED_RESOLUTION,
    ResolutionCapability,
)
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot


class StrategyPositionPhase(str, Enum):
    FLAT = "FLAT"
    ENTRY_PENDING = "ENTRY_PENDING"
    ACTIVE = "ACTIVE"
    EXIT_REQUESTED = "EXIT_REQUESTED"
    EXIT_PENDING = "EXIT_PENDING"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"


@dataclass(frozen=True)
class StrategyLifecycleSnapshot:
    phase: StrategyPositionPhase
    instrument_id: InstrumentId | None
    resolution_committed: bool = False


@dataclass(frozen=True, kw_only=True)
class StrategyContext:
    run_id: RunId
    strategy_id: StrategyId
    market: BinaryMarket


@dataclass(frozen=True, kw_only=True)
class DecisionContext:
    """Immutable context for on_signal — no mutable stores."""

    run_id: RunId
    snapshot: DecisionSnapshot
    target_notional: Decimal
    max_price: Decimal | None = None
    lifecycle: StrategyLifecycleSnapshot | None = None
    position_quantity: Decimal = Decimal("0")
    now: datetime | None = None
    max_hold: timedelta | None = None
    flatten_before_close: timedelta | None = None
    exit_on_flat: bool = True
    kill_switch_active: bool = False
    # R5.1 retry gates (host-owned RetryController)
    entry_allowed: bool = True
    entry_block_reason: str | None = None
    exit_allowed: bool = True
    exit_block_reason: str | None = None
    exit_escalate: bool = False
    exit_urgency: str = "NORMAL"
    # Confirmed execution-session cost basis; zero when flat or unavailable.
    position_cost_total: Decimal = Decimal("0")
    # Host-normalized inventory integrity flag (never guessed by strategy).
    unknown_inventory: bool = False
    # Composition-supplied resolution capability (never inferred by strategy).
    resolution_capability: ResolutionCapability = DISABLED_RESOLUTION
    # True when wall τ is at/inside the configured point-of-no-return window.
    ponr_reached: bool = False
