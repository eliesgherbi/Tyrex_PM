"""`StartupReadinessGate` — deterministic §8 evaluation order."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from tyrex_pm.config.loaders import RiskSettings, RuntimeSettings
from tyrex_pm.runtime.capital import CapitalStateProvider
from tyrex_pm.runtime.capital.policy import CapitalSnapshotPolicy
from tyrex_pm.runtime.lifecycle.exec_predicate import ExecClientsConnected
from tyrex_pm.runtime.lifecycle.instrument_readiness_policy import InstrumentReadinessPolicy
from tyrex_pm.runtime.lifecycle.types import LifecycleReadiness, StartupReadinessResult
from tyrex_pm.runtime.tradable_state import TradableStateHealthSource
from tyrex_pm.runtime.tradable_state.synthetic import synthetic_snapshot_health_source_missing
from tyrex_pm.runtime.tradable_state.types import TradableStateHealth


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class StartupReadinessGate:
    """
    §8 preconditions — **no** venue polling; **no** warmup-as-READY.

    Live **strict READY** requires ``HEALTHY`` tradable health unless product opts into
    ``startup_allow_degraded_live`` → ``DEGRADED`` (``NO_NEW_ENTRIES``) when OMS is degraded
    but other clauses pass.
    """

    def __init__(
        self,
        *,
        runtime: RuntimeSettings,
        risk: RiskSettings,
        capital_provider: CapitalStateProvider | None,
        health_source: TradableStateHealthSource | None,
        cache: Any,
        exec_connected: ExecClientsConnected,
        wallet_sync_ready: Callable[[], bool] | None = None,
        wallet_sync_deadline_exceeded: Callable[[], bool] | None = None,
    ) -> None:
        self._runtime = runtime
        self._risk = risk
        self._capital_provider = capital_provider
        self._health_source = health_source
        self._cache = cache
        self._exec_connected = exec_connected
        self._wallet_sync_ready = wallet_sync_ready
        self._wallet_sync_deadline_exceeded = wallet_sync_deadline_exceeded

    def evaluate(self) -> StartupReadinessResult:
        now = _utc_now()
        rt = self._runtime
        rk = self._risk

        # §8.3 shadow immediate READY (default)
        if rt.execution_mode == "shadow" and not rt.startup_strict_shadow:
            return StartupReadinessResult(
                status=LifecycleReadiness.READY,
                reasons=(),
                evaluated_at_utc=now,
            )

        reasons: list[str] = []

        if not self._exec_connected():
            reasons.append("startup_exec_clients_not_connected")
            return StartupReadinessResult(
                status=LifecycleReadiness.NOT_READY,
                reasons=tuple(reasons),
                evaluated_at_utc=now,
            )

        if self._wallet_sync_ready is not None and not self._wallet_sync_ready():
            if (
                self._wallet_sync_deadline_exceeded is not None
                and self._wallet_sync_deadline_exceeded()
            ):
                reasons.append("startup_wallet_sync_timeout")
            else:
                reasons.append("startup_wallet_sync_pending")
            return StartupReadinessResult(
                status=LifecycleReadiness.NOT_READY,
                reasons=tuple(reasons),
                evaluated_at_utc=now,
            )

        if rk.capital_gate_enabled:
            cap = self._capital_provider
            if cap is None:
                reasons.append("startup_capital_provider_missing")
                return StartupReadinessResult(
                    status=LifecycleReadiness.NOT_READY,
                    reasons=tuple(reasons),
                    evaluated_at_utc=now,
                )
            cap_pol = CapitalSnapshotPolicy.from_risk_settings(rk)
            try:
                st = cap.snapshot(purpose="risk_gate", policy=cap_pol)
            except Exception:  # noqa: BLE001
                reasons.append("startup_capital_snapshot_failed")
                return StartupReadinessResult(
                    status=LifecycleReadiness.NOT_READY,
                    reasons=tuple(reasons),
                    evaluated_at_utc=now,
                )
            if not cap.freshness_ok(st, policy=cap_pol):
                reasons.append("startup_capital_not_fresh")
                return StartupReadinessResult(
                    status=LifecycleReadiness.NOT_READY,
                    reasons=tuple(reasons),
                    evaluated_at_utc=now,
                )

        hs = self._health_source
        if hs is None:
            reasons.append("startup_tradable_health_source_missing")
            h_syn = (
                synthetic_snapshot_health_source_missing(observed_at_utc=now)
                if rk.tradable_state_health_gate_enabled
                else None
            )
            return StartupReadinessResult(
                status=LifecycleReadiness.NOT_READY,
                reasons=tuple(reasons),
                evaluated_at_utc=now,
                health_snapshot=h_syn,
            )
        try:
            h_snap = hs.snapshot()
        except Exception:  # noqa: BLE001
            reasons.append("startup_tradable_health_snapshot_failed")
            return StartupReadinessResult(
                status=LifecycleReadiness.NOT_READY,
                reasons=tuple(reasons),
                evaluated_at_utc=now,
            )

        if h_snap.level == TradableStateHealth.UNKNOWN_BOOTSTRAP:
            reasons.append("startup_tradable_health_unknown_bootstrap")
            return StartupReadinessResult(
                status=LifecycleReadiness.NOT_READY,
                reasons=tuple(reasons),
                evaluated_at_utc=now,
                health_snapshot=h_snap,
            )
        if h_snap.level == TradableStateHealth.DIVERGENT_PERSISTENT:
            reasons.append("startup_tradable_health_divergent_persistent")
            return StartupReadinessResult(
                status=LifecycleReadiness.NOT_READY,
                reasons=tuple(reasons),
                evaluated_at_utc=now,
                health_snapshot=h_snap,
            )

        ok_inst, inst_reason = InstrumentReadinessPolicy(rt).gate_ready(self._cache)
        if not ok_inst:
            if inst_reason:
                reasons.append(inst_reason)
            else:
                reasons.append("startup_instrument_policy_failed")
            return StartupReadinessResult(
                status=LifecycleReadiness.NOT_READY,
                reasons=tuple(reasons),
                evaluated_at_utc=now,
                health_snapshot=h_snap,
            )

        if h_snap.level == TradableStateHealth.DEGRADED_OMS:
            if rt.startup_allow_degraded_live:
                return StartupReadinessResult(
                    status=LifecycleReadiness.DEGRADED,
                    reasons=("startup_degraded_no_new_entries",),
                    evaluated_at_utc=now,
                    health_snapshot=h_snap,
                )
            reasons.append("startup_health_degraded_oms_not_permitted")
            return StartupReadinessResult(
                status=LifecycleReadiness.NOT_READY,
                reasons=tuple(reasons),
                evaluated_at_utc=now,
                health_snapshot=h_snap,
            )

        if h_snap.level == TradableStateHealth.HEALTHY:
            return StartupReadinessResult(
                status=LifecycleReadiness.READY,
                reasons=(),
                evaluated_at_utc=now,
                health_snapshot=h_snap,
            )

        reasons.append("startup_tradable_health_unhandled_level")
        return StartupReadinessResult(
            status=LifecycleReadiness.NOT_READY,
            reasons=tuple(reasons),
            evaluated_at_utc=now,
            health_snapshot=h_snap,
        )
