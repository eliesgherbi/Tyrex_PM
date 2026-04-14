"""Phase 3 — ``StartupReadinessGate`` + ``ExecutionLifecycleStatus`` (``startup_readiness.md``)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from nautilus_trader.model.identifiers import InstrumentId

from tyrex_pm.config.loaders import RiskSettings, RuntimeSettings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.runtime.capital import CapitalStateProvider
from tyrex_pm.runtime.capital.policy import CapitalSnapshotPolicy
from tyrex_pm.runtime.capital.state import CapitalState, CapitalStateSource
from tyrex_pm.runtime.lifecycle import (
    ExecutionLifecycleStatus,
    LifecyclePhase,
    LifecycleReadiness,
    NautilusExecEngineClientsConnected,
    NodeStopGate,
    StartupReadinessCoordinator,
    StartupReadinessGate,
)
from tyrex_pm.runtime.tradable_state import (
    NautilusLiveExecutionHealthSource,
    StaticTradableStateHealthSource,
)
from tyrex_pm.runtime.tradable_state.types import TradableStateHealth, TradableStateHealthSnapshot


def _risk_cap_on(**over: object) -> RiskSettings:
    base: dict[str, object] = {
        "max_notional_usd_per_order": 10.0,
        "max_token_notional_usd_open": float("inf"),
        "kill_switch": False,
        "fail_on_missing_price_for_notional": True,
        "capital_gate_enabled": True,
        "max_account_snapshot_age_seconds": 60.0,
        "max_allowance_snapshot_age_seconds": 120.0,
        "min_collateral_balance_usd": None,
        "min_allowance_usd": None,
        "fail_on_unresolved_token_deployment": False,
        "max_portfolio_notional_usd_open": float("inf"),
        "fail_on_unresolved_portfolio_deployment": True,
        "max_concurrent_guru_resting_orders": None,
        "collateral_reserve_usd": 0.0,
        "min_notional_usd_per_order": 0.0,
        "min_notional_policy": "deny",
        "max_notional_policy": "cap",
        "tradable_state_health_gate_enabled": False,
        "allow_exit_when_degraded_oms": False,
    }
    base.update(over)
    return RiskSettings(**base)  # type: ignore[arg-type]


def _runtime_live(**kwargs: object) -> RuntimeSettings:
    base = {
        "trader_id": "T-TEST-001",
        "execution_mode": "live",
        "guru_poll_interval_seconds": 30.0,
        "data_api_base_url": "https://data-api.polymarket.com",
        "guru_dedup_state_path": "var/d.json",
        "guru_state_path": "var/w.json",
        "guru_activity_limit": 200,
        "guru_startup_backfill_seconds": 0.0,
        "guru_max_activity_pages_per_poll": 4,
        "logging_level": "INFO",
        "clob_host": "https://clob.polymarket.com",
        "chain_id": 137,
        "polymarket_instrument_ids": (),
        "polymarket_token_to_instrument": (),
        "polymarket_dynamic_instruments": True,
        "polymarket_dynamic_max_activations": 32,
        "polymarket_gamma_base_url": "https://gamma-api.polymarket.com",
        "polymarket_gamma_http_timeout_seconds": 15.0,
        "polymarket_startup_token_warmup_max": 0,
        "exec_position_check_interval_secs": None,
        "exec_open_check_interval_secs": None,
        "polymarket_wallet_position_warmup_max": 0,
        "guru_ingest_mode": "poll_only",
        "guru_ingest_phase": "0",
        "guru_rtds_url": "wss://ws-live-data.polymarket.com",
        "guru_rtds_liveness_timeout_seconds": 120.0,
        "guru_rtds_reconnect_retry_initial_seconds": 1.0,
        "guru_rtds_reconnect_retry_max_seconds": 60.0,
        "guru_rtds_ping_interval_seconds": 5.0,
        "guru_poll_fallback_enabled": True,
        "guru_poll_fallback_interval_seconds": None,
        "guru_gap_fill_enabled": True,
        "guru_gap_fill_lookback_seconds": 60.0,
        "guru_proxy_wallet_validation_required": False,
        "guru_stream_queue_drain_interval_ms": 50,
        "execution_entry_guard_enabled": False,
        "execution_max_entry_slippage_ticks": 0,
        "execution_book_depth_clip_enabled": False,
        "execution_book_depth_utilization_cap": 1.0,
        "execution_book_rest_snapshot_enabled": False,
        "execution_book_strict": False,
        "execution_limit_timeout_enabled": False,
        "execution_limit_timeout_seconds": 30.0,
        "reporting_enabled": False,
        "reporting_base_dir": "var/reporting/runs",
        "reporting_sink_max_queue": 50_000,
        "reporting_sink_batch_size": 128,
        "reporting_capital_observability_enabled": True,
        "reporting_capital_snapshot_period_seconds": 300.0,
        "startup_readiness_timeout_seconds": 120.0,
        "startup_strict_shadow": False,
        "startup_allow_degraded_live": False,
        "startup_not_ready_behavior": "exit",
    }
    base.update(kwargs)
    return RuntimeSettings(**base)  # type: ignore[arg-type]


def _h(lev: TradableStateHealth) -> StaticTradableStateHealthSource:
    return StaticTradableStateHealthSource(
        TradableStateHealthSnapshot(
            level=lev,
            reason_code="test",
            observed_at_utc=datetime.now(tz=UTC),
        ),
    )


class _CapOk(CapitalStateProvider):
    def snapshot(self, *, purpose: str, policy: CapitalSnapshotPolicy) -> CapitalState:
        _ = purpose
        now = datetime.now(tz=UTC)
        return CapitalState(
            free_collateral_usd=100.0,
            allowance_usd=None,
            captured_at_utc=now,
            source=CapitalStateSource.ADAPTER_ACCOUNT,
            stale_after_seconds=policy.max_account_snapshot_age_seconds,
            ok=True,
            error=None,
            account_present=True,
            venue="POLYMARKET",
            nautilus_balances={},
            nautilus_cash_free_usd=100.0,
            nautilus_cash_extract_note="test",
            py_clob_balance_usd=None,
            py_clob_allowance_usd=None,
            py_clob_balance_raw=None,
            py_clob_allowance_raw=None,
            py_clob_balance_parse_note="",
            py_clob_allowance_parse_note="",
            merged_clob=False,
        )

    def freshness_ok(self, state: CapitalState, *, policy: CapitalSnapshotPolicy) -> bool:
        _ = state
        _ = policy
        return True


def test_gate_shadow_immediate_ready() -> None:
    rt = _runtime_live(execution_mode="shadow", startup_strict_shadow=False)
    g = StartupReadinessGate(
        runtime=rt,
        risk=_risk_cap_on(),
        capital_provider=None,
        health_source=None,
        cache=MagicMock(),
        exec_connected=lambda: False,
    )
    r = g.evaluate()
    assert r.status == LifecycleReadiness.READY


def test_gate_live_requires_exec_capital_health_instrument() -> None:
    rt = _runtime_live()
    risk = _risk_cap_on()
    cache = MagicMock()
    g = StartupReadinessGate(
        runtime=rt,
        risk=risk,
        capital_provider=_CapOk(),
        health_source=_h(TradableStateHealth.HEALTHY),
        cache=cache,
        exec_connected=lambda: True,
    )
    r = g.evaluate()
    assert r.status == LifecycleReadiness.READY
    assert r.health_snapshot is not None

    g2 = StartupReadinessGate(
        runtime=rt,
        risk=risk,
        capital_provider=_CapOk(),
        health_source=_h(TradableStateHealth.HEALTHY),
        cache=cache,
        exec_connected=lambda: False,
    )
    assert g2.evaluate().status == LifecycleReadiness.NOT_READY


def test_gate_health_source_missing_includes_synthetic_snapshot_when_gate_enabled() -> None:
    """WP4 — startup result carries joinable health snapshot (same deny outcome)."""
    rt = _runtime_live()
    rk = replace(_risk_cap_on(), tradable_state_health_gate_enabled=True)
    g = StartupReadinessGate(
        runtime=rt,
        risk=rk,
        capital_provider=_CapOk(),
        health_source=None,
        cache=MagicMock(),
        exec_connected=lambda: True,
    )
    r = g.evaluate()
    assert r.status == LifecycleReadiness.NOT_READY
    assert "startup_tradable_health_source_missing" in r.reasons
    assert r.health_snapshot is not None
    assert r.health_snapshot.reason_code == "health_source_missing"
    assert r.health_snapshot.level.value == "unknown_bootstrap"


def test_gate_unknown_bootstrap_not_ready() -> None:
    rt = _runtime_live()
    g = StartupReadinessGate(
        runtime=rt,
        risk=_risk_cap_on(),
        capital_provider=_CapOk(),
        health_source=_h(TradableStateHealth.UNKNOWN_BOOTSTRAP),
        cache=MagicMock(),
        exec_connected=lambda: True,
    )
    r = g.evaluate()
    assert r.status == LifecycleReadiness.NOT_READY
    assert "startup_tradable_health_unknown_bootstrap" in r.reasons


def test_gate_degraded_path_requires_flag() -> None:
    rt = _runtime_live(startup_allow_degraded_live=False)
    g = StartupReadinessGate(
        runtime=rt,
        risk=_risk_cap_on(),
        capital_provider=_CapOk(),
        health_source=_h(TradableStateHealth.DEGRADED_OMS),
        cache=MagicMock(),
        exec_connected=lambda: True,
    )
    assert g.evaluate().status == LifecycleReadiness.NOT_READY

    rt2 = _runtime_live(startup_allow_degraded_live=True)
    g2 = StartupReadinessGate(
        runtime=rt2,
        risk=_risk_cap_on(),
        capital_provider=_CapOk(),
        health_source=_h(TradableStateHealth.DEGRADED_OMS),
        cache=MagicMock(),
        exec_connected=lambda: True,
    )
    r2 = g2.evaluate()
    assert r2.status == LifecycleReadiness.DEGRADED


def test_lifecycle_blocks_not_ready_and_degraded_buy() -> None:
    lc = ExecutionLifecycleStatus()
    rk = _risk_cap_on()
    lc.apply_startup_resolution(
        readiness=LifecycleReadiness.NOT_READY,
        phase=LifecyclePhase.READINESS_WAIT,
        entries_allowed=False,
        degraded_definition=None,
        health_snap=None,
        risk_allow_exit_degraded_oms=rk.allow_exit_when_degraded_oms,
    )
    assert lc.block_reason_for_side("BUY", risk=rk) == str(ReasonCode.STARTUP_NOT_READY)
    assert lc.block_reason_for_side("SELL", risk=rk) == str(ReasonCode.STARTUP_NOT_READY)

    snap = TradableStateHealthSnapshot(
        level=TradableStateHealth.DEGRADED_OMS,
        reason_code="t",
        observed_at_utc=datetime.now(tz=UTC),
    )
    lc.apply_startup_resolution(
        readiness=LifecycleReadiness.DEGRADED,
        phase=LifecyclePhase.DEGRADED_LIVE,
        entries_allowed=False,
        degraded_definition="NO_NEW_ENTRIES",
        health_snap=snap,
        risk_allow_exit_degraded_oms=False,
    )
    assert lc.block_reason_for_side("BUY", risk=rk) == str(ReasonCode.STARTUP_DEGRADED_NO_BUY)
    assert lc.block_reason_for_side("SELL", risk=rk) == str(ReasonCode.RISK_HEALTH_DEGRADED_OMS)


class _FakeLiveExecEngine:
    __slots__ = ("_startup_reconciliation_event",)

    def __init__(self) -> None:
        import asyncio

        self._startup_reconciliation_event = asyncio.Event()


def test_startup_gate_nautilus_health_healthy_when_reconciliation_event_set() -> None:
    eng = _FakeLiveExecEngine()
    eng._startup_reconciliation_event.set()
    rt = _runtime_live(execution_mode="live", startup_strict_shadow=True)
    rk = replace(
        _risk_cap_on(),
        tradable_state_health_gate_enabled=True,
        capital_gate_enabled=False,
    )
    gate = StartupReadinessGate(
        runtime=rt,
        risk=rk,
        capital_provider=None,
        health_source=NautilusLiveExecutionHealthSource(eng),
        cache=MagicMock(),
        exec_connected=lambda: True,
    )
    res = gate.evaluate()
    assert res.health_snapshot is not None
    assert res.health_snapshot.level == TradableStateHealth.HEALTHY


def test_startup_gate_ready_with_nautilus_exec_predicate_when_framework_connected() -> None:
    """Wave 2 path: real exec predicate + WP2 health — not stuck on startup_exec_clients_not_connected."""
    eng = MagicMock()
    eng._startup_reconciliation_event = asyncio.Event()
    eng._startup_reconciliation_event.set()
    eng._clients = {"POLYMARKET": object()}
    eng.check_connected = MagicMock(return_value=True)
    rt = _runtime_live(execution_mode="live", startup_strict_shadow=True)
    rk = replace(
        _risk_cap_on(),
        tradable_state_health_gate_enabled=True,
        capital_gate_enabled=False,
    )
    gate = StartupReadinessGate(
        runtime=rt,
        risk=rk,
        capital_provider=None,
        health_source=NautilusLiveExecutionHealthSource(eng),
        cache=MagicMock(),
        exec_connected=NautilusExecEngineClientsConnected(eng),
    )
    res = gate.evaluate()
    assert res.status == LifecycleReadiness.READY
    assert "startup_exec_clients_not_connected" not in res.reasons
    eng.check_connected.assert_called()


def test_startup_gate_exec_predicate_false_blocks_before_health() -> None:
    eng = MagicMock()
    eng._startup_reconciliation_event = asyncio.Event()
    eng._startup_reconciliation_event.set()
    eng._clients = {}
    eng.check_connected = MagicMock(return_value=True)
    rt = _runtime_live(execution_mode="live", startup_strict_shadow=True)
    rk = replace(
        _risk_cap_on(),
        tradable_state_health_gate_enabled=True,
        capital_gate_enabled=False,
    )
    gate = StartupReadinessGate(
        runtime=rt,
        risk=rk,
        capital_provider=None,
        health_source=NautilusLiveExecutionHealthSource(eng),
        cache=MagicMock(),
        exec_connected=NautilusExecEngineClientsConnected(eng),
    )
    res = gate.evaluate()
    assert res.status == LifecycleReadiness.NOT_READY
    assert "startup_exec_clients_not_connected" in res.reasons


def test_coordinator_terminal_requests_exit() -> None:
    rt = _runtime_live(
        startup_readiness_timeout_seconds=0.01,
        startup_not_ready_behavior="exit",
    )
    node = MagicMock()
    gate = StartupReadinessGate(
        runtime=rt,
        risk=_risk_cap_on(),
        capital_provider=_CapOk(),
        health_source=_h(TradableStateHealth.UNKNOWN_BOOTSTRAP),
        cache=MagicMock(),
        exec_connected=lambda: True,
    )
    lc = ExecutionLifecycleStatus()
    rows: list[tuple[str, dict]] = []

    def emit(ft: str, p: dict) -> None:
        rows.append((ft, p))

    coord = StartupReadinessCoordinator(
        node=node,
        gate=gate,
        lifecycle=lc,
        runtime=rt,
        risk=_risk_cap_on(),
        fact_emit=emit,
        run_context=None,
        node_stop_gate=NodeStopGate(),
    )
    import time

    t0 = time.monotonic()
    coord.start_background(t0_mono=t0, deadline_mono=t0 + 0.01)
    time.sleep(0.6)
    coord.stop()
    assert lc.snapshot_view().terminal_not_ready
    assert lc.nonzero_exit_requested
    node.stop.assert_called_once()
    assert any(t == "startup_readiness" and p.get("terminal") for t, p in rows)


def test_apply_startup_resolution_noop_when_shutdown_drain_active() -> None:
    lc = ExecutionLifecycleStatus()
    lc.apply_startup_resolution(
        readiness=LifecycleReadiness.READY,
        phase=LifecyclePhase.LIVE,
        entries_allowed=True,
        degraded_definition=None,
        health_snap=None,
        risk_allow_exit_degraded_oms=False,
        terminal_not_ready=False,
    )
    assert lc.snapshot_view().readiness == LifecycleReadiness.READY
    lc.begin_shutdown_drain(transition_mono=0.0)
    assert lc.snapshot_view().phase == LifecyclePhase.SHUTDOWN_DRAIN
    lc.apply_startup_resolution(
        readiness=LifecycleReadiness.NOT_READY,
        phase=LifecyclePhase.READINESS_WAIT,
        entries_allowed=False,
        degraded_definition=None,
        health_snap=None,
        risk_allow_exit_degraded_oms=False,
        terminal_not_ready=False,
    )
    v = lc.snapshot_view()
    assert v.phase == LifecyclePhase.SHUTDOWN_DRAIN
    assert v.readiness == LifecycleReadiness.READY


def test_startup_worker_yields_immediately_when_shutdown_drain_phase() -> None:
    """WP1 — coordinator must not evaluate readiness after ``begin_shutdown_drain`` (no fact spam)."""
    lc = ExecutionLifecycleStatus()
    lc.begin_shutdown_drain(transition_mono=0.0)
    rt = _runtime_live(startup_readiness_timeout_seconds=300.0)
    node = MagicMock()
    gate = MagicMock()
    gate.evaluate.return_value = MagicMock(
        status=LifecycleReadiness.NOT_READY,
        reasons=("would_run_forever",),
        health_snapshot=None,
    )
    emitted: list[tuple[str, dict]] = []

    coord = StartupReadinessCoordinator(
        node=node,
        gate=gate,
        lifecycle=lc,
        runtime=rt,
        risk=_risk_cap_on(),
        fact_emit=lambda ft, p: emitted.append((ft, p)),
        run_context=None,
        node_stop_gate=NodeStopGate(),
    )
    import time

    t0 = time.monotonic()
    coord.start_background(t0_mono=t0, deadline_mono=t0 + 999.0)
    time.sleep(0.15)
    coord.stop()
    gate.evaluate.assert_not_called()
    assert not emitted


def test_node_stop_gate_idempotent() -> None:
    node = MagicMock()
    gate = NodeStopGate()
    log = MagicMock()
    gate.stop_node(node, log=log)
    gate.stop_node(node, log=log)
    node.stop.assert_called_once()


def test_static_instrument_must_be_in_cache() -> None:
    from tyrex_pm.runtime.lifecycle.instrument_policy import static_instruments_in_cache

    iid = "0xabc-1.POLYMARKET"
    _ = InstrumentId.from_str(iid)
    cache = MagicMock()
    cache.instrument.return_value = None
    ok, reason = static_instruments_in_cache(cache, (iid,))
    assert not ok
    assert reason == "startup_instrument_missing_from_cache"
    cache.instrument.return_value = object()
    ok2, _ = static_instruments_in_cache(cache, (iid,))
    assert ok2


# ---------------------------------------------------------------------------
# Step 6: Wallet sync clause (06_tests.md §3)
# ---------------------------------------------------------------------------


class TestGateWalletSync:
    def test_wallet_sync_ready_none_no_change(self) -> None:
        """When wallet_sync_ready is None, gate does not add wallet sync checks."""
        gate = StartupReadinessGate(
            runtime=_runtime_live(),
            risk=_risk_cap_on(capital_gate_enabled=False),
            capital_provider=None,
            health_source=_h(TradableStateHealth.HEALTHY),
            cache=MagicMock(),
            exec_connected=lambda: True,
            wallet_sync_ready=None,
        )
        r = gate.evaluate()
        assert r.status == LifecycleReadiness.READY

    def test_wallet_sync_pending(self) -> None:
        gate = StartupReadinessGate(
            runtime=_runtime_live(),
            risk=_risk_cap_on(capital_gate_enabled=False),
            capital_provider=None,
            health_source=_h(TradableStateHealth.HEALTHY),
            cache=MagicMock(),
            exec_connected=lambda: True,
            wallet_sync_ready=lambda: False,
            wallet_sync_deadline_exceeded=lambda: False,
        )
        r = gate.evaluate()
        assert r.status == LifecycleReadiness.NOT_READY
        assert "startup_wallet_sync_pending" in r.reasons

    def test_wallet_sync_timeout(self) -> None:
        gate = StartupReadinessGate(
            runtime=_runtime_live(),
            risk=_risk_cap_on(capital_gate_enabled=False),
            capital_provider=None,
            health_source=_h(TradableStateHealth.HEALTHY),
            cache=MagicMock(),
            exec_connected=lambda: True,
            wallet_sync_ready=lambda: False,
            wallet_sync_deadline_exceeded=lambda: True,
        )
        r = gate.evaluate()
        assert r.status == LifecycleReadiness.NOT_READY
        assert "startup_wallet_sync_timeout" in r.reasons

    def test_wallet_sync_ready_passes_through(self) -> None:
        gate = StartupReadinessGate(
            runtime=_runtime_live(),
            risk=_risk_cap_on(capital_gate_enabled=False),
            capital_provider=None,
            health_source=_h(TradableStateHealth.HEALTHY),
            cache=MagicMock(),
            exec_connected=lambda: True,
            wallet_sync_ready=lambda: True,
        )
        r = gate.evaluate()
        assert r.status == LifecycleReadiness.READY

    def test_wallet_sync_ready_but_capital_fails(self) -> None:
        """Wallet sync pass does not short-circuit other checks."""
        gate = StartupReadinessGate(
            runtime=_runtime_live(),
            risk=_risk_cap_on(capital_gate_enabled=True),
            capital_provider=None,
            health_source=_h(TradableStateHealth.HEALTHY),
            cache=MagicMock(),
            exec_connected=lambda: True,
            wallet_sync_ready=lambda: True,
        )
        r = gate.evaluate()
        assert r.status == LifecycleReadiness.NOT_READY
        assert "startup_capital_provider_missing" in r.reasons
