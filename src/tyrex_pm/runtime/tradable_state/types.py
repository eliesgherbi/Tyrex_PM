"""Tradable OMS/cache trust level — framework-sourced only (see planning docs)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class TradableStateHealth(str, Enum):
    """
    Trust in Nautilus OMS/cache for Polymarket live.

    Values and risk matrix: ``Docs/Implementation/refactor_lifecycle/tradable_state_health.md`` §10.
    """

    HEALTHY = "healthy"
    UNKNOWN_BOOTSTRAP = "unknown_bootstrap"
    DEGRADED_OMS = "degraded_oms"
    DIVERGENT_PERSISTENT = "divergent_persistent"


@dataclass(frozen=True, slots=True)
class TradableStateHealthSnapshot:
    """Immutable health view — produced only from typed framework inputs (or explicit stubs in tests)."""

    level: TradableStateHealth
    reason_code: str
    observed_at_utc: datetime
    framework_detail: str | None = None
