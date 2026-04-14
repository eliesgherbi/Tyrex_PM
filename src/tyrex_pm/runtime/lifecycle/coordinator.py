"""
§8.5 — concurrent readiness evaluation while ``TradingNode.run`` blocks.

Uses a **daemon thread** (§14.2 codable path) until a Nautilus actor hook replaces it.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from tyrex_pm.config.loaders import RiskSettings, RuntimeSettings
from tyrex_pm.runtime.lifecycle.gate import StartupReadinessGate
from tyrex_pm.runtime.lifecycle.node_stop_gate import NodeStopGate
from tyrex_pm.runtime.lifecycle.status import ExecutionLifecycleStatus
from tyrex_pm.runtime.lifecycle.types import LifecyclePhase, LifecycleReadiness

_LOG = logging.getLogger(__name__)


def _emit_startup_fact(
    emit: Callable[[str, dict[str, Any]], None] | None,
    *,
    status: str,
    reasons: tuple[str, ...],
    timeout_seconds: float,
    mode: str,
    t0_mono: float,
    deadline_mono: float,
    terminal: bool,
    duration_ms: float | None,
) -> None:
    if emit is None:
        return
    payload: dict[str, Any] = {
        "status": status,
        "reasons": list(reasons),
        "timeout_seconds": timeout_seconds,
        "mode": mode,
        "t0_mono": t0_mono,
        "deadline_mono": deadline_mono,
        "terminal": terminal,
    }
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    emit("startup_readiness", payload)


class StartupReadinessCoordinator:
    """
    Polls :class:`StartupReadinessGate` until READY / DEGRADED / terminal NOT_READY.

    On terminal NOT_READY with ``startup_not_ready_behavior: exit``, calls ``node.stop()``
    from the worker thread so ``node.run()`` unwinds (§8.5.2).
    """

    __slots__ = (
        "_node",
        "_node_stop_gate",
        "_gate",
        "_lifecycle",
        "_runtime",
        "_risk",
        "_emit",
        "_run_context",
        "_thread",
        "_stop",
        "_last_emitted_key",
    )

    def __init__(
        self,
        *,
        node: Any,
        gate: StartupReadinessGate,
        lifecycle: ExecutionLifecycleStatus,
        runtime: RuntimeSettings,
        risk: RiskSettings,
        fact_emit: Callable[[str, dict[str, Any]], None] | None,
        run_context: Any | None,
        node_stop_gate: NodeStopGate | None = None,
    ) -> None:
        self._node = node
        self._node_stop_gate = node_stop_gate
        self._gate = gate
        self._lifecycle = lifecycle
        self._runtime = runtime
        self._risk = risk
        self._emit = fact_emit
        self._run_context = run_context
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_emitted_key: str | None = None

    def apply_shadow_immediate_ready(self, *, t0_mono: float, deadline_mono: float) -> None:
        """§8.3 — single-shot READY without background polling."""
        self._lifecycle.apply_startup_resolution(
            readiness=LifecycleReadiness.READY,
            phase=LifecyclePhase.LIVE,
            entries_allowed=True,
            degraded_definition=None,
            health_snap=None,
            risk_allow_exit_degraded_oms=self._risk.allow_exit_when_degraded_oms,
            terminal_not_ready=False,
            transition_mono=time.monotonic(),
        )
        dur_ms = (time.monotonic() - t0_mono) * 1000.0
        key = "READY:shadow"
        if self._last_emitted_key != key:
            self._last_emitted_key = key
            _emit_startup_fact(
                self._emit,
                status="READY",
                reasons=(),
                timeout_seconds=self._runtime.startup_readiness_timeout_seconds,
                mode="shadow_immediate",
                t0_mono=t0_mono,
                deadline_mono=deadline_mono,
                terminal=False,
                duration_ms=dur_ms,
            )
        rc = self._run_context
        if rc is not None:
            rc.update_manifest_fields(
                startup_readiness_status="READY",
                startup_duration_ms=dur_ms,
            )

    def start_background(self, *, t0_mono: float, deadline_mono: float) -> None:
        def worker() -> None:
            poll_s = 0.25
            while not self._stop.is_set():
                # WP1 — if drain began (e.g. slow ``coord.stop()`` join), never clobber shutdown phase.
                if self._lifecycle.snapshot_view().phase == LifecyclePhase.SHUTDOWN_DRAIN:
                    return
                now_m = time.monotonic()
                res = self._gate.evaluate()
                hs = res.health_snapshot
                dur_ms = (now_m - t0_mono) * 1000.0

                if res.status == LifecycleReadiness.READY:
                    self._lifecycle.apply_startup_resolution(
                        readiness=LifecycleReadiness.READY,
                        phase=LifecyclePhase.LIVE,
                        entries_allowed=True,
                        degraded_definition=None,
                        health_snap=hs,
                        risk_allow_exit_degraded_oms=self._risk.allow_exit_when_degraded_oms,
                        terminal_not_ready=False,
                        transition_mono=now_m,
                    )
                    key = "READY"
                    if self._last_emitted_key != key:
                        self._last_emitted_key = key
                        _emit_startup_fact(
                            self._emit,
                            status="READY",
                            reasons=res.reasons,
                            timeout_seconds=self._runtime.startup_readiness_timeout_seconds,
                            mode=self._runtime.execution_mode,
                            t0_mono=t0_mono,
                            deadline_mono=deadline_mono,
                            terminal=False,
                            duration_ms=dur_ms,
                        )
                    rc = self._run_context
                    if rc is not None:
                        rc.update_manifest_fields(
                            startup_readiness_status="READY",
                            startup_duration_ms=dur_ms,
                        )
                    return

                if res.status == LifecycleReadiness.DEGRADED:
                    self._lifecycle.apply_startup_resolution(
                        readiness=LifecycleReadiness.DEGRADED,
                        phase=LifecyclePhase.DEGRADED_LIVE,
                        entries_allowed=False,
                        degraded_definition="NO_NEW_ENTRIES",
                        health_snap=hs,
                        risk_allow_exit_degraded_oms=self._risk.allow_exit_when_degraded_oms,
                        terminal_not_ready=False,
                        transition_mono=now_m,
                    )
                    key = "DEGRADED"
                    if self._last_emitted_key != key:
                        self._last_emitted_key = key
                        _emit_startup_fact(
                            self._emit,
                            status="DEGRADED",
                            reasons=res.reasons,
                            timeout_seconds=self._runtime.startup_readiness_timeout_seconds,
                            mode=self._runtime.execution_mode,
                            t0_mono=t0_mono,
                            deadline_mono=deadline_mono,
                            terminal=False,
                            duration_ms=dur_ms,
                        )
                    rc = self._run_context
                    if rc is not None:
                        rc.update_manifest_fields(
                            startup_readiness_status="DEGRADED",
                            startup_duration_ms=dur_ms,
                        )
                    return

                if now_m > deadline_mono:
                    self._lifecycle.apply_startup_resolution(
                        readiness=LifecycleReadiness.NOT_READY,
                        phase=LifecyclePhase.NO_TRADE,
                        entries_allowed=False,
                        degraded_definition=None,
                        health_snap=hs,
                        risk_allow_exit_degraded_oms=self._risk.allow_exit_when_degraded_oms,
                        terminal_not_ready=True,
                        transition_mono=now_m,
                        nonzero_exit_requested=self._runtime.startup_not_ready_behavior == "exit",
                    )
                    key = "NOT_READY:terminal"
                    if self._last_emitted_key != key:
                        self._last_emitted_key = key
                        _emit_startup_fact(
                            self._emit,
                            status="NOT_READY",
                            reasons=res.reasons + ("startup_deadline_exceeded",),
                            timeout_seconds=self._runtime.startup_readiness_timeout_seconds,
                            mode=self._runtime.execution_mode,
                            t0_mono=t0_mono,
                            deadline_mono=deadline_mono,
                            terminal=True,
                            duration_ms=dur_ms,
                        )
                    rc = self._run_context
                    if rc is not None:
                        rc.update_manifest_fields(
                            startup_readiness_status="NOT_READY",
                            startup_duration_ms=dur_ms,
                        )
                    if self._runtime.startup_not_ready_behavior == "exit":
                        if self._node_stop_gate is not None:
                            self._node_stop_gate.stop_node(self._node, log=_LOG)
                        else:
                            try:
                                self._node.stop()
                            except Exception as exc:  # noqa: BLE001
                                _LOG.warning(
                                    "startup terminal NOT_READY: node.stop failed: %s",
                                    exc,
                                )
                    return

                self._lifecycle.apply_startup_resolution(
                    readiness=LifecycleReadiness.NOT_READY,
                    phase=LifecyclePhase.READINESS_WAIT,
                    entries_allowed=False,
                    degraded_definition=None,
                    health_snap=hs,
                    risk_allow_exit_degraded_oms=self._risk.allow_exit_when_degraded_oms,
                    terminal_not_ready=False,
                    transition_mono=now_m,
                )
                key = f"NOT_READY:{':'.join(res.reasons)}" if res.reasons else "NOT_READY"
                if self._last_emitted_key != key:
                    self._last_emitted_key = key
                    _emit_startup_fact(
                        self._emit,
                        status="NOT_READY",
                        reasons=res.reasons,
                        timeout_seconds=self._runtime.startup_readiness_timeout_seconds,
                        mode=self._runtime.execution_mode,
                        t0_mono=t0_mono,
                        deadline_mono=deadline_mono,
                        terminal=False,
                        duration_ms=None,
                    )

                self._stop.wait(poll_s)

        self._thread = threading.Thread(
            target=worker,
            name="tyrex_startup_readiness",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=2.0)
