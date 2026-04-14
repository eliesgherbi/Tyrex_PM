"""Phase 4 — shutdown drain coordinator (``shutdown_drain.md``)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from unittest.mock import MagicMock, patch

import pytest
from nautilus_trader.model.identifiers import InstrumentId, StrategyId

from tyrex_pm.config.loaders import RuntimeSettings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.runtime.lifecycle.shutdown_drain import ShutdownDrainCoordinator, ShutdownDrainResult
from tyrex_pm.runtime.lifecycle.status import ExecutionLifecycleStatus
from tyrex_pm.runtime.lifecycle.types import LifecyclePhase
from tyrex_pm.runtime.state_readers import OrderSnapshot


def _live_runtime(**kwargs: object) -> RuntimeSettings:
    base = RuntimeSettings(
        trader_id="T-001",
        execution_mode="live",
        guru_poll_interval_seconds=30.0,
        data_api_base_url="https://data-api.polymarket.com",
        guru_dedup_state_path="var/guru_dedup.json",
        guru_state_path="var/guru_watermark.json",
        guru_activity_limit=200,
        guru_startup_backfill_seconds=0.0,
        guru_max_activity_pages_per_poll=4,
        logging_level="INFO",
        clob_host="https://clob.polymarket.com",
        chain_id=137,
        polymarket_instrument_ids=(),
        polymarket_token_to_instrument=(),
        polymarket_dynamic_instruments=True,
        polymarket_dynamic_max_activations=32,
        polymarket_gamma_base_url="https://gamma-api.polymarket.com",
        polymarket_gamma_http_timeout_seconds=15.0,
        polymarket_startup_token_warmup_max=32,
    )
    return replace(base, **kwargs) if kwargs else base


def _snap(cid: str, inst: str = "0xabc-12345.POLYMARKET") -> OrderSnapshot:
    return OrderSnapshot(
        client_order_id=cid,
        venue_order_id="v1",
        status="ACTIVE",
        side="BUY",
        quantity="1",
        leaves_quantity="1",
        price="0.5",
        instrument_id=inst,
    )


@dataclass
class _SeqReader:
    _batches: list[tuple[OrderSnapshot, ...]]

    def list_open_orders_for_strategy(self, **_: object) -> tuple[OrderSnapshot, ...]:
        if not self._batches:
            return ()
        return self._batches.pop(0)


@dataclass
class _MockStrategy:
    strategy_id: StrategyId
    cancel_calls: list[str]
    fail_instruments: frozenset[str] = frozenset()

    @property
    def id(self) -> StrategyId:
        return self.strategy_id

    def cancel_all_orders(self, instrument_id: object, **_: object) -> None:
        key = str(instrument_id)
        if key in self.fail_instruments:
            raise RuntimeError(f"cancel failed for {key}")
        self.cancel_calls.append(key)


def test_skip_reason_operator_override_manifest_unclean() -> None:
    r = ShutdownDrainResult(
        skipped=True,
        skip_reason="operator_override",
        canceled_order_count=0,
        residual_open_orders=(),
        timed_out=False,
        drain_duration_ms=0,
        instruments_cancelled=0,
        cancel_failures=(),
    )
    assert not r.manifest_clean()


def test_coordinator_shadow_skips() -> None:
    rt = _live_runtime(execution_mode="shadow")
    lc = ExecutionLifecycleStatus()
    strat = _MockStrategy(StrategyId("S-DRAIN-001"), [])
    reader = _SeqReader([])
    coord = ShutdownDrainCoordinator(rt, poll_interval_s=0.0)
    r = coord.run(
        strategy=strat,
        lifecycle=lc,
        execution_reader=reader,  # type: ignore[arg-type]
        fact_emit=None,
        run_id=None,
        run_context=None,
    )
    assert r.skipped and r.skip_reason == "shadow_mode"
    assert lc.snapshot_view().phase != LifecyclePhase.SHUTDOWN_DRAIN


def test_coordinator_cancel_failure_still_emits_terminal_result_and_fact() -> None:
    rt = _live_runtime(
        shutdown_drain_enabled=True,
        shutdown_drain_timeout_seconds=30.0,
        shutdown_drain_override=False,
    )
    lc = ExecutionLifecycleStatus()
    inst_bad = "0xbad-1.POLYMARKET"
    inst_ok = "0xabc-12345.POLYMARKET"
    batches = [
        (_snap("a", inst_bad), _snap("b", inst_ok)),
        (),
    ]
    reader = _SeqReader(batches)
    strat = _MockStrategy(
        StrategyId("S-DRAIN-001"),
        [],
        fail_instruments=frozenset({inst_bad}),
    )
    coord = ShutdownDrainCoordinator(rt, poll_interval_s=0.0)
    facts: list[tuple[str, dict]] = []

    def emit(ft: str, payload: dict) -> None:
        facts.append((ft, payload))

    r = coord.run(
        strategy=strat,
        lifecycle=lc,
        execution_reader=reader,  # type: ignore[arg-type]
        fact_emit=emit,
        run_id="run-x",
        run_context=None,
    )
    assert len(facts) == 1 and facts[0][0] == "shutdown_drain"
    assert facts[0][1]["cancel_partial_failure"] is True
    assert inst_bad in facts[0][1]["cancel_failures"][0]
    assert r.cancel_failures
    assert not r.manifest_clean()
    assert strat.cancel_calls == [inst_ok]


def test_coordinator_live_sets_shutdown_phase_before_cancel() -> None:
    rt = _live_runtime(
        shutdown_drain_enabled=True,
        shutdown_drain_timeout_seconds=30.0,
        shutdown_drain_override=False,
    )
    lc = ExecutionLifecycleStatus()
    batches = [
        (_snap("a"),),
        (),
    ]
    reader = _SeqReader(batches)
    strat = _MockStrategy(StrategyId("S-DRAIN-001"), [])
    coord = ShutdownDrainCoordinator(rt, poll_interval_s=0.0)
    coord.run(
        strategy=strat,
        lifecycle=lc,
        execution_reader=reader,  # type: ignore[arg-type]
        fact_emit=None,
        run_id=None,
        run_context=None,
    )
    assert lc.snapshot_view().phase == LifecyclePhase.SHUTDOWN_DRAIN
    assert strat.cancel_calls == ["0xabc-12345.POLYMARKET"]


def test_coordinator_timeout_leaves_residuals(monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _live_runtime(
        shutdown_drain_enabled=True,
        shutdown_drain_timeout_seconds=30.0,
        shutdown_drain_override=False,
    )
    lc = ExecutionLifecycleStatus()
    stuck = (_snap("x"),)

    class _StuckReader:
        def list_open_orders_for_strategy(self, **_: object) -> tuple[OrderSnapshot, ...]:
            return stuck

    reader = _StuckReader()
    strat = _MockStrategy(StrategyId("S-DRAIN-001"), [])
    coord = ShutdownDrainCoordinator(rt, poll_interval_s=0.0)

    mono_vals = [0.0, 0.0, 100.0, 100.0]
    mi = 0

    def fake_mono() -> float:
        nonlocal mi
        v = mono_vals[mi] if mi < len(mono_vals) else 100.0
        mi += 1
        return v

    monkeypatch.setattr("tyrex_pm.runtime.lifecycle.shutdown_drain.time.monotonic", fake_mono)
    r = coord.run(
        strategy=strat,
        lifecycle=lc,
        execution_reader=reader,  # type: ignore[arg-type]
        fact_emit=None,
        run_id=None,
        run_context=None,
    )
    assert r.timed_out
    assert r.residual_open_orders == ("x",)
    assert not r.manifest_clean()


def test_block_reason_shutdown_drain_blocks_buy_and_sell() -> None:
    from tyrex_pm.config.loaders import RiskSettings
    from tyrex_pm.runtime.lifecycle.types import LifecyclePhase, LifecycleReadiness

    risk = RiskSettings(
        max_notional_usd_per_order=1.0,
        max_token_notional_usd_open=float("inf"),
        kill_switch=False,
        fail_on_missing_price_for_notional=True,
    )
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
    lc.begin_shutdown_drain()
    assert lc.block_reason_for_side("BUY", risk=risk) == str(ReasonCode.SHUTDOWN_DRAIN_ACTIVE)
    assert lc.block_reason_for_side("SELL", risk=risk) == str(ReasonCode.SHUTDOWN_DRAIN_ACTIVE)


def test_shutdown_drain_fact_payload_validates() -> None:
    from tyrex_pm.reporting.schema.facts_v1 import fact_envelope

    row = fact_envelope(
        fact_type="shutdown_drain",
        run_id="r1",
        recorded_at_utc="2026-04-10T00:00:00+00:00",
        payload={
            "skipped": False,
            "skip_reason": "",
            "timed_out": False,
            "residual_count": 0,
            "canceled_count": 1,
            "drain_duration_ms": 12,
            "residual_client_order_ids": [],
            "instruments_cancelled": 1,
            "cancel_failures": [],
            "cancel_partial_failure": False,
            "internal_error": "",
            "drain_aborted_internal": False,
        },
    )
    assert row["fact_type"] == "shutdown_drain"


def test_coordinator_list_open_initial_raises_emits_terminal_fact_and_internal_error() -> None:
    rt = _live_runtime(
        shutdown_drain_enabled=True,
        shutdown_drain_timeout_seconds=30.0,
        shutdown_drain_override=False,
    )
    lc = ExecutionLifecycleStatus()

    class _BrokenReader:
        def list_open_orders_for_strategy(self, **_: object) -> tuple[OrderSnapshot, ...]:
            raise RuntimeError("cache exploded")

    strat = _MockStrategy(StrategyId("S-DRAIN-001"), [])
    coord = ShutdownDrainCoordinator(rt, poll_interval_s=0.0)
    facts: list[tuple[str, dict]] = []

    def emit(ft: str, payload: dict) -> None:
        facts.append((ft, payload))

    r = coord.run(
        strategy=strat,
        lifecycle=lc,
        execution_reader=_BrokenReader(),  # type: ignore[arg-type]
        fact_emit=emit,
        run_id="run-x",
        run_context=None,
    )
    assert len(facts) == 1
    assert facts[0][1]["drain_aborted_internal"] is True
    assert "cache exploded" in (facts[0][1].get("internal_error") or "")
    assert r.internal_error
    assert not r.manifest_clean()


def test_coordinator_poll_raises_contains_cancel_failure_manifest_dirty() -> None:
    rt = _live_runtime(
        shutdown_drain_enabled=True,
        shutdown_drain_timeout_seconds=30.0,
        shutdown_drain_override=False,
    )
    lc = ExecutionLifecycleStatus()
    batches = [
        (_snap("a"),),
    ]

    class _FlakyReader:
        _n = 0

        def list_open_orders_for_strategy(self, **_: object) -> tuple[OrderSnapshot, ...]:
            _FlakyReader._n += 1
            if _FlakyReader._n == 1:
                return batches[0]
            raise ConnectionError("mid-poll")

    strat = _MockStrategy(StrategyId("S-DRAIN-001"), [])
    coord = ShutdownDrainCoordinator(rt, poll_interval_s=0.0)
    r = coord.run(
        strategy=strat,
        lifecycle=lc,
        execution_reader=_FlakyReader(),  # type: ignore[arg-type]
        fact_emit=None,
        run_id=None,
        run_context=None,
    )
    assert any(x.startswith("poll:") for x in r.cancel_failures)
    assert not r.manifest_clean()


def test_coordinator_live_body_raises_still_emits_shutdown_drain_fact() -> None:
    rt = _live_runtime(
        shutdown_drain_enabled=True,
        shutdown_drain_timeout_seconds=30.0,
        shutdown_drain_override=False,
    )
    lc = ExecutionLifecycleStatus()
    strat = _MockStrategy(StrategyId("S-DRAIN-001"), [])
    reader = _SeqReader([(_snap("a"),), ()])

    coord = ShutdownDrainCoordinator(rt, poll_interval_s=0.0)
    facts: list[tuple[str, dict]] = []

    with patch.object(
        ShutdownDrainCoordinator,
        "_live_drain_body",
        side_effect=ValueError("inject after cancel"),
    ):
        r = coord.run(
            strategy=strat,
            lifecycle=lc,
            execution_reader=reader,  # type: ignore[arg-type]
            fact_emit=lambda ft, p: facts.append((ft, p)),
            run_id="r1",
            run_context=None,
        )
    assert len(facts) == 1 and facts[0][0] == "shutdown_drain"
    assert facts[0][1]["drain_aborted_internal"] is True
    assert r.internal_error == "inject after cancel"


def test_drain_before_node_stop_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    from tyrex_pm.runtime import guru_shutdown

    called: dict[str, object] = {}

    def fake_run(self: object, **kwargs: object) -> ShutdownDrainResult:
        called["kwargs"] = kwargs
        return ShutdownDrainResult(
            skipped=True,
            skip_reason="unit",
            canceled_order_count=0,
            residual_open_orders=(),
            timed_out=False,
            drain_duration_ms=0,
            instruments_cancelled=0,
            cancel_failures=(),
        )

    monkeypatch.setattr(
        "tyrex_pm.runtime.lifecycle.shutdown_drain.ShutdownDrainCoordinator.run",
        fake_run,
    )
    assembly = MagicMock()
    assembly.guru_strategy = MagicMock()
    assembly.execution_lifecycle = MagicMock()
    assembly.execution_state = MagicMock()
    rt = _live_runtime()
    rc = MagicMock()
    rc.run_id = "rid"
    rc.emit = MagicMock()
    guru_shutdown.drain_before_node_stop(assembly, rt, rc)
    assert "kwargs" in called
    kw = called["kwargs"]
    assert kw["run_id"] == "rid"
    assert kw["fact_emit"] is rc.emit
