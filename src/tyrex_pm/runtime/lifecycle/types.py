"""Startup readiness DTOs — ``startup_readiness.md`` §7–§8."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal

from tyrex_pm.runtime.tradable_state.types import TradableStateHealthSnapshot


class LifecycleReadiness(str, Enum):
    """Frozen §8.1 — readiness outcome of the gate."""

    READY = "READY"
    NOT_READY = "NOT_READY"
    DEGRADED = "DEGRADED"


class LifecyclePhase(str, Enum):
    """High-level process phase for operators / manifest (not a second policy table)."""

    READINESS_WAIT = "readiness_wait"
    LIVE = "live"
    DEGRADED_LIVE = "degraded_live"
    NO_TRADE = "no_trade"
    #: Phase 4 — ``shutdown_drain.md`` §8: entries off + cancel/drain before ``node.stop()``.
    SHUTDOWN_DRAIN = "shutdown_drain"


DegradedDefinition = Literal["NO_NEW_ENTRIES"]


@dataclass(frozen=True, slots=True)
class StartupReadinessResult:
    """§7 contract — deterministic evaluation snapshot."""

    status: LifecycleReadiness
    reasons: tuple[str, ...]
    evaluated_at_utc: datetime
    #: Present when this evaluation read tradable health (for lifecycle / §8.4 SELL).
    health_snapshot: TradableStateHealthSnapshot | None = None
