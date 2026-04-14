"""
Explicit non-production stubs for tests and pre-spike live wiring.

Do **not** use log parsing or reconciliation reimplementation here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tyrex_pm.runtime.tradable_state.provider import TradableStateHealthSource
from tyrex_pm.runtime.tradable_state.types import TradableStateHealth, TradableStateHealthSnapshot

_UNKNOWN_REASON = "no_framework_signal_phase2_spike_pending"


class StaticTradableStateHealthSource:
    """Fixed snapshot — for unit tests and deterministic harnesses."""

    __slots__ = ("_snap",)

    def __init__(self, snap: TradableStateHealthSnapshot) -> None:
        self._snap = snap

    def snapshot(self) -> TradableStateHealthSnapshot:
        return self._snap


class UnknownBootstrapHealthSource:
    """
    Explicit ``UNKNOWN_BOOTSTRAP`` producer for **readiness** when the tradable health gate is off
    but live/strict-shadow still needs a source (§8), or for tests.

    When ``tradable_state_health_gate_enabled`` is **true**, compose wires
    :class:`~tyrex_pm.runtime.tradable_state.nautilus_live_health.NautilusLiveExecutionHealthSource`
    instead (WP2).
    """

    __slots__ = ()

    def snapshot(self) -> TradableStateHealthSnapshot:
        return TradableStateHealthSnapshot(
            level=TradableStateHealth.UNKNOWN_BOOTSTRAP,
            reason_code=_UNKNOWN_REASON,
            observed_at_utc=datetime.now(tz=UTC),
            framework_detail=None,
        )
