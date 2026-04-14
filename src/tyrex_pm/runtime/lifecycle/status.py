"""Thread-safe `ExecutionLifecycleStatus` — ``startup_readiness.md`` §5 / §7."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from tyrex_pm.config.loaders import RiskSettings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.runtime.lifecycle.types import (
    DegradedDefinition,
    LifecyclePhase,
    LifecycleReadiness,
)
from tyrex_pm.runtime.tradable_state import tradable_health_allows_intent
from tyrex_pm.runtime.tradable_state.types import TradableStateHealthSnapshot


@dataclass(frozen=True, slots=True)
class _LifecycleView:
    readiness: LifecycleReadiness
    phase: LifecyclePhase
    entries_allowed: bool
    degraded_definition: DegradedDefinition | None
    terminal_not_ready: bool
    health_snap: TradableStateHealthSnapshot | None
    risk_allow_exit_degraded_oms: bool


class ExecutionLifecycleStatus:
    """
    Shared holder updated by :class:`~tyrex_pm.runtime.lifecycle.coordinator.StartupReadinessCoordinator`
    and read by strategies.

    ``entries_allowed`` means **BUY / new entries** only; **SELL** under ``DEGRADED`` follows
    ``tradable_state_health.md`` §10 via :meth:`block_reason_for_side`.
    """

    __slots__ = (
        "_lock",
        "_readiness",
        "_phase",
        "_entries_allowed",
        "_degraded_definition",
        "_terminal_not_ready",
        "_health_snap",
        "_risk_allow_exit_degraded_oms",
        "_nonzero_exit_requested",
        "_transition_mono",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._readiness = LifecycleReadiness.NOT_READY
        self._phase = LifecyclePhase.READINESS_WAIT
        self._entries_allowed = False
        self._degraded_definition: DegradedDefinition | None = None
        self._terminal_not_ready = False
        self._health_snap: TradableStateHealthSnapshot | None = None
        self._risk_allow_exit_degraded_oms = False
        self._nonzero_exit_requested = False
        self._transition_mono: float | None = None

    def snapshot_view(self) -> _LifecycleView:
        with self._lock:
            return _LifecycleView(
                readiness=self._readiness,
                phase=self._phase,
                entries_allowed=self._entries_allowed,
                degraded_definition=self._degraded_definition,
                terminal_not_ready=self._terminal_not_ready,
                health_snap=self._health_snap,
                risk_allow_exit_degraded_oms=self._risk_allow_exit_degraded_oms,
            )

    def begin_shutdown_drain(self, *, transition_mono: float | None = None) -> None:
        """Phase 4 — ``shutdown_drain.md`` §8.2: disable entries; block **both** sides until lifted."""
        with self._lock:
            self._phase = LifecyclePhase.SHUTDOWN_DRAIN
            self._entries_allowed = False
            if transition_mono is not None:
                self._transition_mono = transition_mono

    def apply_startup_resolution(
        self,
        *,
        readiness: LifecycleReadiness,
        phase: LifecyclePhase,
        entries_allowed: bool,
        degraded_definition: DegradedDefinition | None,
        health_snap: TradableStateHealthSnapshot | None,
        risk_allow_exit_degraded_oms: bool,
        terminal_not_ready: bool = False,
        transition_mono: float | None = None,
        nonzero_exit_requested: bool = False,
    ) -> None:
        with self._lock:
            # WP1 — ``phase4_followup.md`` C: startup worker must not clobber ``SHUTDOWN_DRAIN``.
            if self._phase == LifecyclePhase.SHUTDOWN_DRAIN:
                return
            self._readiness = readiness
            self._phase = phase
            self._entries_allowed = entries_allowed
            self._degraded_definition = degraded_definition
            self._health_snap = health_snap
            self._risk_allow_exit_degraded_oms = risk_allow_exit_degraded_oms
            self._terminal_not_ready = terminal_not_ready
            if transition_mono is not None:
                self._transition_mono = transition_mono
            if nonzero_exit_requested:
                self._nonzero_exit_requested = True

    @property
    def nonzero_exit_requested(self) -> bool:
        with self._lock:
            return self._nonzero_exit_requested

    @property
    def transition_mono(self) -> float | None:
        with self._lock:
            return self._transition_mono

    def block_reason_for_side(self, side_upper: str, *, risk: RiskSettings) -> str | None:
        """
        Return a ``ReasonCode`` string to block strategy submit, or ``None`` if startup allows
        this side through to risk/execution.

        ``risk`` supplies ``allow_exit_when_degraded_oms`` for §8.4 SELL under ``DEGRADED``.
        """
        v = self.snapshot_view()
        side = side_upper.upper()

        if v.phase == LifecyclePhase.SHUTDOWN_DRAIN:
            return str(ReasonCode.SHUTDOWN_DRAIN_ACTIVE)

        if v.readiness == LifecycleReadiness.NOT_READY:
            return (
                str(ReasonCode.STARTUP_TERMINAL_NOT_READY)
                if v.terminal_not_ready
                else str(ReasonCode.STARTUP_NOT_READY)
            )

        if v.readiness == LifecycleReadiness.DEGRADED:
            if side == "BUY":
                return str(ReasonCode.STARTUP_DEGRADED_NO_BUY)
            if v.health_snap is None:
                return str(ReasonCode.STARTUP_DEGRADED_HEALTH_MISSING)
            ok, rc = tradable_health_allows_intent(
                v.health_snap,
                side_upper=side,
                allow_exit_when_degraded_oms=risk.allow_exit_when_degraded_oms,
            )
            if ok:
                return None
            return rc

        if v.readiness == LifecycleReadiness.READY:
            return None

        return str(ReasonCode.STARTUP_NOT_READY)
