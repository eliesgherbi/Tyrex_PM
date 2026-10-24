from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from support.runtime import bare_runtime, stand_in_runtime

import tyrex_pm.runtime.trading_runtime as trading_runtime_module
from tyrex_pm.application.cli import main
from tyrex_pm.core.ids import CorrelationId, InstrumentId, MarketId, StrategyId
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.intents import EnterIntent, ExitIntent, new_intent_id
from tyrex_pm.execution.account_state import AccountSnapshotStatus
from tyrex_pm.execution.coordinator import (
    AccountExecutionCoordinator,
    GatewaySubmissionResult,
    PreparedOrder,
)
from tyrex_pm.execution.lifecycle import ExecutionLifecycle, LifecyclePolicy
from tyrex_pm.execution.planner import ExecutionRiskPolicy, IntentOrderPlanner
from tyrex_pm.execution.reconciliation import SessionReconciler
from tyrex_pm.execution.session_state import ExecutionPhase, ExecutionSessionState
from tyrex_pm.persistence.execution_journal import MemoryExecutionJournal
from tyrex_pm.persistence.run_evidence_journal import RunEvidenceRecord
from tyrex_pm.reporting.run_report import RunReportInput, build_run_report
from tyrex_pm.runtime.capabilities import CapabilityController
from tyrex_pm.runtime.market_data_runtime import MarketDataSummary
from tyrex_pm.runtime.run_config import load_trading_run_config
from tyrex_pm.runtime.trading_runtime import TradingRuntime

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def _entry() -> EnterIntent:
    return EnterIntent(
        intent_id=new_intent_id(),
        strategy_id=StrategyId("z_gap"),
        instrument_id=InstrumentId("yes"),
        market_id=MarketId("market"),
        created_at=NOW,
        correlation_id=CorrelationId("correlation"),
        causation_id=None,
        reason_code="ENTRY_CANDIDATE",
        target_notional=Decimal("5"),
        outcome=OutcomeSide.YES,
        max_price=Decimal("0.51"),
    )


def _exit() -> ExitIntent:
    return ExitIntent(
        intent_id=new_intent_id(),
        strategy_id=StrategyId("z_gap"),
        instrument_id=InstrumentId("yes"),
        market_id=MarketId("market"),
        created_at=NOW,
        correlation_id=CorrelationId("correlation"),
        causation_id=None,
        reason_code="THESIS_EXIT",
    )


def test_d1_tiny_live_loads_with_effective_cutoff() -> None:
    from tyrex_pm.runtime.run_config import (
        effective_entry_cutoff_s,
        host_safety_floor_s,
        strategy_entry_cutoff_s,
    )

    cfg = load_trading_run_config(Path("config/runs/z_gap_tiny_live.yaml"))
    assert host_safety_floor_s(cfg) == 105.0
    assert effective_entry_cutoff_s(cfg) == 105.0
    assert cfg.strategy.entry.tau_min_s == 105.0
    assert cfg.market.entry_dispatch_margin_s == cfg.market.evaluation_interval_s == 1.0
    assert strategy_entry_cutoff_s(cfg) == 106.0


def test_d2_tau_min_below_host_floor_fails_load(tmp_path: Path) -> None:
    from tyrex_pm.runtime.run_config import RunConfigError

    text = Path("config/runs/z_gap_tiny_live.yaml").read_text(encoding="utf-8")
    path = tmp_path / "low_tau.yaml"
    path.write_text(text.replace("tau_min_s: 105.0", "tau_min_s: 10.0"), encoding="utf-8")
    with pytest.raises(RunConfigError, match="host safety floor"):
        load_trading_run_config(path)


def test_single_run_config_is_strict_and_keeps_tiny_live_cap() -> None:
    cfg = load_trading_run_config(Path("config/runs/z_gap_tiny_live.yaml"))
    assert cfg.strategy_kind == "z_gap"
    assert cfg.risk.maximum_total_debit == Decimal("5")
    assert cfg.lifecycle.mandatory_exit_before_end_s > cfg.lifecycle.manual_deadline_before_end_s
    assert cfg.lifecycle.dust_abs_shares == Decimal("0.01")
    assert cfg.lifecycle.dust_max_notional == Decimal("0.05")
    assert cfg.lifecycle.prepare_budget_ms == 2000
    assert cfg.lifecycle.refresh_wait_budget_ms == 500
    assert cfg.lifecycle.prepare_budget_ms < cfg.market.maximum_candidate_age_ms
    assert cfg.protection is not None
    assert cfg.protection.take_profit is not None
    assert cfg.protection.take_profit.style == "resting_gtd"


def test_z_gap_loads_with_and_without_protection(tmp_path: Path) -> None:
    with_protection = load_trading_run_config(Path("config/runs/z_gap_tiny_live.yaml"))
    assert with_protection.protection is not None
    text = Path("config/runs/z_gap_tiny_live.yaml").read_text(encoding="utf-8")
    start = text.index("\nprotection:")
    end = text.index("\nmarket:")
    stripped = text[:start] + text[end:]
    path = tmp_path / "z_gap_no_protection.yaml"
    path.write_text(stripped, encoding="utf-8")
    without = load_trading_run_config(path)
    assert without.strategy_kind == "z_gap"
    assert without.protection is None


def test_cli_validates_single_config_without_network(capsys) -> None:
    assert TradingRuntime.__name__ == "TradingRuntime"
    code = main(
        [
            "run",
            "--config",
            "config/runs/z_gap_tiny_live.yaml",
            "--validate-config",
        ]
    )
    assert code == 0
    assert "config ok strategy=z_gap" in capsys.readouterr().out


def test_capabilities_keep_exit_separate_from_entry_readiness() -> None:
    caps = CapabilityController(
        live_requested=True,
        public_feeds_ready=True,
        active_market_ready=True,
        books_ready=True,
        model_ready=True,
        account_reads_ready=True,
        user_stream_ready=False,
        collateral_ready=False,
        entry_allowance_ready=False,
        selected_token_sellable=True,
        prior_scope_clear=False,
        entry_window_open=False,
    )
    result = caps.snapshot(exposed=True)
    assert not result.entry_executable
    assert not result.execution_infrastructure_ready
    assert result.exit_executable
    assert result.reconciliation_ready


def test_model_lockout_does_not_clear_execution_infrastructure() -> None:
    caps = CapabilityController(
        live_requested=True,
        public_feeds_ready=True,
        active_market_ready=True,
        books_ready=True,
        model_ready=False,
        account_reads_ready=True,
        user_stream_ready=True,
        collateral_ready=True,
        entry_allowance_ready=True,
        order_metadata_ready=True,
        prior_scope_clear=True,
        entry_window_open=True,
    )
    result = caps.snapshot()
    assert result.execution_infrastructure_ready
    assert not result.entry_executable
    assert not result.decision_ready
    assert "MODEL_NOT_READY" in result.blockers
    assert "ACCOUNT_READS_NOT_READY" not in result.blockers


def test_order_metadata_blocks_entry_until_warm() -> None:
    caps = CapabilityController(
        live_requested=True,
        public_feeds_ready=True,
        active_market_ready=True,
        books_ready=True,
        model_ready=True,
        account_reads_ready=True,
        user_stream_ready=True,
        collateral_ready=True,
        entry_allowance_ready=True,
        order_metadata_ready=False,
        prior_scope_clear=True,
        entry_window_open=True,
    )
    result = caps.snapshot()
    assert not result.entry_executable
    assert not result.execution_infrastructure_ready
    assert "ORDER_METADATA_NOT_READY" in result.blockers
    caps.order_metadata_ready = True
    ready = caps.snapshot()
    assert ready.entry_executable
    assert ready.execution_infrastructure_ready
    assert "ORDER_METADATA_NOT_READY" not in ready.blockers


def test_exit_capability_uses_fresh_dispatch_gate_not_cached_book_health() -> None:
    caps = CapabilityController(
        live_requested=True,
        active_market_ready=True,
        account_reads_ready=True,
        selected_token_sellable=True,
        books_ready=False,
        book_desync_active=True,
    )
    result = caps.snapshot(exposed=True)
    assert result.exit_executable
    assert not result.entry_executable
    assert not result.execution_infrastructure_ready


@pytest.mark.asyncio
async def test_degraded_public_data_does_not_starve_queued_exit() -> None:
    consumed = asyncio.Event()

    async def _consume() -> None:
        if runtime.intent_queue.empty():
            return
        runtime.intent_queue.get_nowait()
        runtime.intent_queue.task_done()
        consumed.set()

    runtime = stand_in_runtime(
        intent_queue=asyncio.Queue(),
        intent_wake=asyncio.Event(),
        _intent_dispatcher_stop=asyncio.Event(),
        _consume_intent=_consume,
    )
    runtime.intent_queue.put_nowait(_exit())
    runtime.intent_wake.set()
    await runtime.intent_wake.wait()
    runtime.intent_wake.clear()
    while not runtime.intent_queue.empty():
        await runtime._consume_intent()
    assert consumed.is_set()


@pytest.mark.asyncio
async def test_intent_enqueue_wakes_dispatcher_without_one_hertz_tick() -> None:
    consumed = asyncio.Event()

    async def _consume(self) -> None:  # noqa: ANN001
        if self.intent_queue.empty():
            return
        self.intent_queue.get_nowait()
        self.intent_queue.task_done()
        consumed.set()

    runtime = bare_runtime()
    runtime.intent_queue = asyncio.Queue()
    runtime.intent_wake = asyncio.Event()
    runtime.intent_candidate_monotonic_ns = {}
    runtime._intent_dispatcher_stop = asyncio.Event()
    runtime._intent_dispatcher_task = None
    runtime._consume_intent = _consume.__get__(runtime, TradingRuntime)
    runtime._start_intent_dispatcher()
    try:
        started = time.monotonic()
        TradingRuntime._enqueue_intent(runtime, _exit())
        await asyncio.wait_for(consumed.wait(), timeout=0.5)
        assert (time.monotonic() - started) < 0.45
    finally:
        await TradingRuntime._stop_intent_dispatcher(runtime)


@pytest.mark.asyncio
async def test_manual_deadline_is_enforced_with_open_exposure() -> None:
    state = SimpleNamespace(
        has_exposure=True,
        needs_venue_followup=False,
        exit_requested_reason="MANDATORY_FLATTEN",
    )
    lifecycle = SimpleNamespace(state=state, mark_manual=AsyncMock())
    runtime = stand_in_runtime(
        _bind_pending_market=AsyncMock(),
        _market_data_ready=lambda: False,
        active_market=SimpleNamespace(
            entry_enabled=True,
            market=SimpleNamespace(
                event_end=NOW,
                condition_id="condition",
                market_id=SimpleNamespace(value="market"),
            ),
        ),
        account_state_authority=SimpleNamespace(current=lambda _market_id: object()),
        _apply_account_snapshot=Mock(),
        config=SimpleNamespace(
            strategy_kind="z_gap",
            strategy=SimpleNamespace(entry=SimpleNamespace(tau_min_s=0, tau_max_s=300)),
            lifecycle=SimpleNamespace(
                mandatory_exit_before_end_s=90,
                manual_deadline_before_end_s=45,
                evidence_timeout_s=15,
            ),
            market=SimpleNamespace(entry_dispatch_margin_s=1.0),
            protection=None,
        ),
        capabilities=SimpleNamespace(entry_window_open=False),
        lifecycle=lifecycle,
        intent_queue=asyncio.Queue(),
        _consume_intent=AsyncMock(),
        _maybe_arm_protection=Mock(),
        _maybe_emit_protection_exit=Mock(),
        _refresh_order_metadata_capability=Mock(),
        _maybe_refresh_active_order_metadata=Mock(),
        selected_instrument=None,
        stop_requested=False,
        time_authority=SimpleNamespace(now_corrected_utc=lambda: NOW),
        _current_entry_tau_s=lambda: 0.0,
    )
    market_runtime = SimpleNamespace(
        zgap=SimpleNamespace(
            time_authority=SimpleNamespace(now_corrected_utc=lambda: NOW),
        )
    )
    await TradingRuntime.on_async_tick(runtime, market_runtime)
    lifecycle.mark_manual.assert_awaited_once_with("manual_deadline_reached_with_exposure")
    assert runtime.stop_requested


class _Venue:
    def __init__(self) -> None:
        self.balance = Decimal("0")
        self.posts: list[str] = []
        self.trades: list[SimpleNamespace] = []

    async def prepare_order(self, spec):  # noqa: ANN001
        return PreparedOrder(spec.order_id, spec.order_id, spec)

    async def post_order(self, prepared):  # noqa: ANN001
        spec = prepared.payload
        venue_id = "venue-entry" if spec.side.value == "BUY" else f"venue-exit-{len(self.posts)}"
        self.posts.append(spec.side.value)
        shares = Decimal("10") if spec.side.value == "BUY" else spec.shares
        self.balance += shares if spec.side.value == "BUY" else -shares
        self.trades.append(
            SimpleNamespace(
                id=f"trade-{len(self.trades)}",
                taker_order_id=venue_id,
                maker_orders=(),
                status="CONFIRMED",
                side=spec.side.value,
                size=shares,
                price=Decimal("0.49"),
                updated_at=NOW,
            )
        )
        return GatewaySubmissionResult(
            accepted=True,
            venue_order_id=venue_id,
            status="matched",
            cumulative_matched_shares=shares,
            trade_ids=(self.trades[-1].id,),
        )

    async def discard_prepared(self, prepared):  # noqa: ANN001
        return None

    async def list_open_orders(self, **_kwargs):
        return ()

    async def list_account_trades(self, **_kwargs):
        return tuple(self.trades)

    async def conditional_balance(self, _token_id):
        return self.balance, Decimal("100")

    async def close(self):
        return None

    def startup_inspection(self):
        from tyrex_pm.execution.startup_reconciliation import (
            AccountIdentity,
            InspectionFacts,
            ReadCoverage,
        )

        return InspectionFacts(
            identity=AccountIdentity(
                chain_id=137,
                environment="production",
                wallet="0x1111111111111111111111111111111111111111",
            ),
            coverage=ReadCoverage(
                complete=True,
                positions_complete=True,
                orders_complete=True,
                activity_complete=True,
            ),
            closing_holdings=(),
            closing_open_orders=(),
        )


@pytest.mark.asyncio
async def test_async_production_shape_runs_entry_and_exit_to_flat() -> None:
    venue = _Venue()
    capabilities = CapabilityController(
        live_requested=True,
        public_feeds_ready=True,
        active_market_ready=True,
        books_ready=True,
        model_ready=True,
        account_reads_ready=True,
        user_stream_ready=True,
        collateral_ready=True,
        entry_allowance_ready=True,
        order_metadata_ready=True,
        prior_scope_clear=True,
        entry_window_open=True,
        selected_token_sellable=True,
    )
    coordinator = AccountExecutionCoordinator(
        journal=MemoryExecutionJournal(),
        gateway=venue,
        final_gate=lambda _spec: asyncio.sleep(0, result=(True, None)),
    )
    lifecycle = ExecutionLifecycle(
        coordinator=coordinator,
        reconciler=SessionReconciler(venue),
        planner=IntentOrderPlanner(ExecutionRiskPolicy(maximum_total_debit=Decimal("5"))),
        capabilities=capabilities,
        policy=LifecyclePolicy(
            evidence_timeout_s=1,
            reconciliation_interval_s=0.001,
            exit_retry_limit=2,
            exit_retry_budget_s=1,
        ),
    )
    await lifecycle.open(
        session_id="session",
        strategy_id="z_gap",
        market_id="market",
        window_id="window",
        token_id="token",
        baseline_position_shares=Decimal("0"),
        baseline_sellable_shares=Decimal("0"),
    )
    state = await lifecycle.submit_entry(_entry(), token_id="token")
    assert state.phase is ExecutionPhase.POSITION_OPEN
    assert state.confirmed_position_shares == Decimal("10")
    state = await lifecycle.submit_exit(
        _exit(),
        token_id="token",
        min_order_size=Decimal("1"),
        last_bid=Decimal("0.49"),
    )
    assert state.phase is ExecutionPhase.COMPLETED_FLAT
    assert venue.posts == ["BUY", "SELL"]
    assert coordinator.mutation_attempt_count("session") == 2
    timeline = TradingRuntime._build_execution_timeline(
        SimpleNamespace(coordinator=coordinator), state
    )
    assert [row["role"] for row in timeline] == ["ENTRY", "EXIT"]
    assert all(row["http_post_started_at"] for row in timeline)
    assert all(row["post_round_trip_ms"] is not None for row in timeline)
    assert all(0 <= row["candidate_to_request_ms"] < 1_000 for row in timeline)
    await coordinator.close()


@pytest.mark.asyncio
async def test_known_entry_preparation_failure_returns_without_reconciliation_timeout() -> None:
    class FailedVenue(_Venue):
        async def prepare_order(self, spec):  # noqa: ANN001
            raise ValueError("invalid local order preparation")

    venue = FailedVenue()
    capabilities = CapabilityController(live_requested=True)
    coordinator = AccountExecutionCoordinator(
        journal=MemoryExecutionJournal(),
        gateway=venue,
        final_gate=lambda _spec: asyncio.sleep(0, result=(True, None)),
    )
    lifecycle = ExecutionLifecycle(
        coordinator=coordinator,
        reconciler=SessionReconciler(venue),
        planner=IntentOrderPlanner(ExecutionRiskPolicy(maximum_total_debit=Decimal("5"))),
        capabilities=capabilities,
        policy=LifecyclePolicy(evidence_timeout_s=30),
    )
    await lifecycle.open(
        session_id="session",
        strategy_id="z_gap",
        market_id="market",
        window_id="window",
        token_id="token",
        baseline_position_shares=Decimal("0"),
        baseline_sellable_shares=Decimal("0"),
    )

    state = await asyncio.wait_for(lifecycle.submit_entry(_entry(), token_id="token"), 0.1)
    assert state.phase is ExecutionPhase.COMPLETED_NO_DISPATCH
    assert state.last_error is not None
    assert coordinator.mutation_attempt_count("session") == 0
    timeline = TradingRuntime._build_execution_timeline(
        SimpleNamespace(coordinator=coordinator), state
    )
    report = build_run_report(
        RunReportInput(
            run_instance_id="run",
            configured_run_name="test",
            live_requested=True,
            state=state,
            mutation_attempts=0,
            execution_timeline=timeline,
            capabilities={},
            market_data=None,
            run_evidence=(),
            runtime_errors=(),
        )
    )
    assert report["outcome"] == "COMPLETED_NO_DISPATCH"
    assert report["ok"] is True
    assert report["summary"]["observed_fills_complete"] is True
    assert report["summary"]["reconciliation_complete"] is True
    assert report["validation_scope"]["furthest_execution_stage"] == ("ENTRY_PREPARATION_FAILED")
    assert report["validation_scope"]["entry_http_post_attempted"] is False
    assert timeline[0]["pre_dispatch_failure"]["stage"] == "PREPARATION"
    assert timeline[0]["http_post_started_at"] is None
    await coordinator.close()


def test_market_data_summary_makes_no_execution_authority_claims() -> None:
    payload = MarketDataSummary(
        run_id="run",
        out_dir="out",
        started_at=NOW.isoformat(),
    ).to_dict()
    forbidden = {
        "auth_touched",
        "orders_touched",
        "oms_touched",
        "venue_mutation",
        "orders_live",
        "in_session_lifecycle",
        "evaluations",
        "evaluation_count",
    }
    assert forbidden.isdisjoint(payload)


def test_entry_evaluation_refreshes_account_projection_first() -> None:
    snapshot = object()
    runtime = stand_in_runtime(
        active_market=SimpleNamespace(
            market=SimpleNamespace(
                condition_id="condition",
                market_id=SimpleNamespace(value="market"),
            )
        ),
        account_state_authority=SimpleNamespace(current=Mock(return_value=snapshot)),
        _apply_account_snapshot=Mock(),
        _market_data_ready=Mock(return_value=True),
    )
    assert TradingRuntime._entry_evaluation_ready(runtime)
    runtime.account_state_authority.current.assert_called_once_with("condition")
    runtime._apply_account_snapshot.assert_called_once_with(snapshot)
    runtime._market_data_ready.assert_called_once_with()


async def _reporting_runtime(tmp_path: Path) -> TradingRuntime:
    config = replace(
        load_trading_run_config(Path("config/runs/z_gap_tiny_live.yaml")),
        state_directory=tmp_path / "state",
        report_directory=tmp_path / "runs",
    )
    return await TradingRuntime.create(
        config=config,
        output_directory=tmp_path / "output",
        env={},
        gateway=_Venue(),
    )


async def _completed_market_data(**kwargs) -> MarketDataSummary:  # noqa: ANN003
    return MarketDataSummary(
        run_id=str(kwargs["run_id"]),
        out_dir=str(kwargs["out_dir"]),
        started_at=NOW.isoformat(),
        ended_at=NOW.isoformat(),
    )


@pytest.mark.asyncio
async def test_unified_report_is_written_after_normal_composition_shutdown(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = await _reporting_runtime(tmp_path)
    runtime.run_recorder.record(
        "STRATEGY_DECISION",
        {"action": "WAIT", "reason_code": "INSUFFICIENT_EDGE"},
    )
    runtime.run_recorder.record(
        "READINESS_CHANGED",
        {"entry_executable": True, "blockers": []},
    )
    monkeypatch.setattr(
        trading_runtime_module,
        "run_market_data_runtime",
        _completed_market_data,
    )
    result = await runtime.run()
    payload = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert result.outcome == "COMPLETED_NO_ENTRY_SIGNAL"
    assert payload["no_entry_diagnosis"] == {
        "reason": "STRATEGY_EVALUATED_NO_ECONOMIC_SIGNAL",
        "furthest_stage": "ECONOMIC_DECISION",
        "execution_infrastructure_ready_ever": True,
        "entry_executable_ever": True,
        "decision_ready_ever": False,
        "strategy_inputs_eligible_ever": True,
        "strategy_inputs_eligible_count": 1,
        "strategy_decision_count": 1,
        "intent_emitted_count": 0,
        "strategy_input_blocker_counts": {},
        "last_readiness_blockers": [],
    }
    assert payload["schema_version"] == 6
    assert payload["run_evidence_omitted"] == {"STRATEGY_DECISION": 1}
    assert all(r["event_type"] != "STRATEGY_DECISION" for r in payload["run_evidence"])
    assert payload["mutation_attempts"] == 0
    assert payload["run_evidence_summary"]["strategy_reason_counts"] == {"INSUFFICIENT_EDGE": 1}
    assert payload["reporting"]["execution_authority"] == "sqlite.execution_events"
    assert payload["validation_scope"]["execution_lifecycle_exercised"] is False


@pytest.mark.asyncio
async def test_decision_recording_is_started_closed_and_reported(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = await _reporting_runtime(tmp_path)
    monkeypatch.setattr(trading_runtime_module, "run_market_data_runtime", _completed_market_data)
    result = await runtime.run()
    payload = json.loads(result.report_path.read_text(encoding="utf-8"))
    recording = payload["recording"]
    assert recording["level"] == "decisions" and recording["failed"] is False
    assert recording["streams"]["decisions"]["state"] == "closed"
    assert recording["writer_finished"] is True
    assert recording["run_evidence"]["dropped"] == {}
    folder = tmp_path / "output" / "recording"
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "live" and manifest["run_id"] == runtime.run_instance_id
    assert (folder / "status.json").exists()
    events = [r["event_type"] for r in payload["run_evidence"]]
    assert "RECORDING_STARTED" in events and "RECORDING_CLOSED" in events


@pytest.mark.asyncio
async def test_strategy_input_blocked_is_not_reported_as_no_signal(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = await _reporting_runtime(tmp_path)
    runtime.run_recorder.record(
        "READINESS_CHANGED",
        {"entry_executable": True, "decision_ready": True, "blockers": []},
    )
    runtime.run_recorder.record(
        "STRATEGY_DECISION",
        {
            "action": "WAIT",
            "reason_code": "TIME_NOT_READY",
            "strategy_inputs_eligible": False,
            "strategy_blockers": ["TIME_NOT_READY", "PTB_NOT_USABLE"],
        },
    )
    monkeypatch.setattr(trading_runtime_module, "run_market_data_runtime", _completed_market_data)
    result = await runtime.run()
    payload = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert result.outcome == "NO_ENTRY_STRATEGY_INPUT_BLOCKED"
    assert result.ok is False
    diagnosis = payload["no_entry_diagnosis"]
    assert diagnosis["reason"] == "STRATEGY_INPUTS_NEVER_ELIGIBLE"
    assert diagnosis["strategy_input_blocker_counts"] == {
        "PTB_NOT_USABLE": 1,
        "TIME_NOT_READY": 1,
    }


@pytest.mark.asyncio
async def test_runtime_failure_still_writes_authoritative_failure_report(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = await _reporting_runtime(tmp_path)

    async def fail_market_data(**_kwargs):
        raise AttributeError("secondary market summary failed")

    monkeypatch.setattr(
        trading_runtime_module,
        "run_market_data_runtime",
        fail_market_data,
    )
    result = await runtime.run()
    payload = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert result.outcome == "RUNTIME_FAILURE"
    assert not result.ok
    assert payload["fatal_error"]["error_type"] == "AttributeError"
    assert payload["mutation_attempts"] == 0
    assert payload["run_evidence_summary"]["event_counts"]["RUNTIME_ERROR"] == 1


@pytest.mark.asyncio
async def test_no_entry_report_fails_when_runtime_never_became_entry_executable(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = await _reporting_runtime(tmp_path)
    runtime.run_recorder.record(
        "READINESS_CHANGED",
        {
            "decision_ready": True,
            "execution_infrastructure_ready": False,
            "entry_executable": False,
            "blockers": ["ACCOUNT_READS_NOT_READY", "ACCOUNT_STATE_AUTHORITY"],
        },
    )
    monkeypatch.setattr(
        trading_runtime_module,
        "run_market_data_runtime",
        _completed_market_data,
    )
    result = await runtime.run()
    payload = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert result.outcome == "NO_ENTRY_RUNTIME_BLOCKED"
    assert not result.ok
    assert payload["no_entry_diagnosis"]["reason"] == "RUNTIME_NEVER_ENTRY_EXECUTABLE"
    assert payload["no_entry_diagnosis"]["execution_infrastructure_ready_ever"] is False
    assert payload["no_entry_diagnosis"]["last_readiness_blockers"] == [
        "ACCOUNT_READS_NOT_READY",
        "ACCOUNT_STATE_AUTHORITY",
    ]


@pytest.mark.asyncio
async def test_model_lockout_with_ready_infrastructure_is_strategy_input_blocked(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = await _reporting_runtime(tmp_path)
    runtime.run_recorder.record(
        "READINESS_CHANGED",
        {
            "decision_ready": False,
            "execution_infrastructure_ready": True,
            "entry_executable": False,
            "blockers": ["MODEL_NOT_READY"],
        },
    )
    runtime.run_recorder.record(
        "STRATEGY_DECISION",
        {
            "action": "WAIT",
            "reason_code": "JUMP_GUARD",
            "strategy_inputs_eligible": False,
            "strategy_blockers": ["JUMP_GUARD", "MODEL_NOT_READY"],
        },
    )
    monkeypatch.setattr(trading_runtime_module, "run_market_data_runtime", _completed_market_data)
    result = await runtime.run()
    payload = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert result.outcome == "NO_ENTRY_STRATEGY_INPUT_BLOCKED"
    assert result.ok is False
    diagnosis = payload["no_entry_diagnosis"]
    assert diagnosis["reason"] == "STRATEGY_INPUTS_NEVER_ELIGIBLE"
    assert diagnosis["furthest_stage"] == "STRATEGY_INPUT_QUALITY"
    assert diagnosis["execution_infrastructure_ready_ever"] is True
    assert diagnosis["entry_executable_ever"] is False
    assert diagnosis["strategy_input_blocker_counts"] == {
        "JUMP_GUARD": 1,
        "MODEL_NOT_READY": 1,
    }


@pytest.mark.asyncio
async def test_emitted_entry_without_session_is_not_reported_as_no_signal(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = await _reporting_runtime(tmp_path)
    runtime.run_recorder.record(
        "READINESS_CHANGED",
        {"decision_ready": True, "entry_executable": True, "blockers": []},
    )
    runtime.run_recorder.record(
        "INTENT_EMITTED",
        {"intent_id": "entry-intent", "intent_type": "EnterIntent"},
    )
    runtime.run_recorder.record(
        "INTENT_BLOCKED",
        {
            "intent_id": "entry-intent",
            "reason": "ACCOUNT_STATE_NOT_AUTHORITATIVE",
        },
    )
    monkeypatch.setattr(
        trading_runtime_module,
        "run_market_data_runtime",
        _completed_market_data,
    )
    result = await runtime.run()
    payload = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert result.outcome == "ENTRY_ABORTED_BEFORE_SESSION"
    assert not result.ok
    assert payload["no_entry_diagnosis"]["intent_emitted_count"] == 1
    assert payload["no_entry_diagnosis"]["reason"] == "ENTRY_INTENT_DID_NOT_CREATE_SESSION"


@pytest.mark.asyncio
async def test_report_projection_failure_writes_emergency_report(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = await _reporting_runtime(tmp_path)
    monkeypatch.setattr(
        trading_runtime_module,
        "run_market_data_runtime",
        _completed_market_data,
    )
    import tyrex_pm.runtime.host.reporting as reporting_module

    monkeypatch.setattr(
        reporting_module,
        "build_run_report",
        lambda _source: (_ for _ in ()).throw(ValueError("projection failed")),
    )
    result = await runtime.run()
    payload = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert result.outcome == "RUNTIME_FAILURE"
    assert payload["reporting"]["emergency"] is True
    assert payload["reporting"]["error_type"] == "ValueError"
    assert payload["mutation_attempts"] == 0


@pytest.mark.asyncio
async def test_r2_nonzero_clock_correction_aligns_tick_consume_and_gate() -> None:
    from tyrex_pm.core.clock import FakeClock
    from tyrex_pm.core.time_authority import (
        ClockSyncSnapshot,
        SnapshotTimeAuthority,
        TimeSyncStatus,
    )
    from tyrex_pm.execution.final_gate import FinalExecutionGate
    from tyrex_pm.runtime.run_config import effective_entry_cutoff_s, strategy_entry_cutoff_s

    config = load_trading_run_config(Path("config/runs/z_gap_tiny_live.yaml"))
    event_end = datetime(2026, 9, 20, 12, 5, tzinfo=timezone.utc)
    wall = datetime(2026, 9, 20, 12, 3, 20, tzinfo=timezone.utc)
    clock = FakeClock(_wall=wall)
    auth = SnapshotTimeAuthority(clock=clock, max_uncertainty_ms=10_000)
    auth.apply_snapshot(
        ClockSyncSnapshot(
            measured_at_wall_utc=wall,
            measured_at_monotonic_ns=clock.monotonic_ns(),
            estimated_offset_ms=20_000.0,
            uncertainty_ms=10,
            sync_status=TimeSyncStatus.READY,
            primary_source="test",
        )
    )
    runtime = bare_runtime()
    runtime.config = config
    runtime.time_authority = auth
    runtime.active_market = SimpleNamespace(
        market=SimpleNamespace(event_end=event_end),
        entry_enabled=True,
    )
    tau = runtime._current_entry_tau_s()
    uncorrected = (event_end - wall).total_seconds()
    assert tau == pytest.approx(uncorrected - 20.0)
    hard = effective_entry_cutoff_s(config)
    soft = strategy_entry_cutoff_s(config)
    assert soft == hard + config.market.entry_dispatch_margin_s
    tick_open = bool(tau is not None and soft <= tau <= 290.0)
    consume_open = bool(tau is not None and tau >= hard)
    assert tick_open is consume_open
    assert tau < hard
    gate = FinalExecutionGate(
        capture_book=lambda: None,
        capabilities=CapabilityController(live_requested=True, entry_window_open=True),
        active_binding_id=lambda: "b",
    )
    gate.entry_cutoff_s = hard
    gate.entry_tau_s = runtime._current_entry_tau_s
    from tyrex_pm.execution.orders import MarketBuyOrderSpec

    spec = MarketBuyOrderSpec(
        order_id="e",
        market_id="m",
        instrument_id="yes",
        token_id="yes",
        spend_amount=Decimal("5"),
        maximum_total_debit=Decimal("5"),
        worst_price=Decimal("0.6"),
        metadata={"candidate_monotonic_ns": time.monotonic_ns()},
    )
    allowed, reason = await gate(spec)
    assert allowed is False
    assert reason == "ENTRY_CUTOFF_PASSED"


def _window_runtime(
    *, tau: float, exposed: bool
) -> tuple[SimpleNamespace, SimpleNamespace, list, Mock]:
    enqueued: list = []
    protection = Mock()
    state = SimpleNamespace(
        has_exposure=exposed,
        needs_venue_followup=False,
        exit_requested_reason=None,
        session_id="session",
    )
    runtime = stand_in_runtime(
        _bind_pending_market=AsyncMock(),
        _market_data_ready=lambda: False,
        active_market=SimpleNamespace(
            entry_enabled=True,
            market=SimpleNamespace(
                event_end=NOW,
                condition_id="condition",
                market_id=SimpleNamespace(value="market"),
            ),
        ),
        account_state_authority=SimpleNamespace(current=lambda _market_id: object()),
        _apply_account_snapshot=Mock(),
        config=SimpleNamespace(
            strategy_kind="z_gap",
            strategy=SimpleNamespace(entry=SimpleNamespace(tau_min_s=105.0, tau_max_s=290.0)),
            lifecycle=SimpleNamespace(
                mandatory_exit_before_end_s=90,
                manual_deadline_before_end_s=45,
                evidence_timeout_s=15,
            ),
            market=SimpleNamespace(entry_dispatch_margin_s=1.0),
            protection=None,
        ),
        capabilities=SimpleNamespace(entry_window_open=False),
        lifecycle=SimpleNamespace(state=state, mark_manual=AsyncMock()),
        intent_queue=asyncio.Queue(),
        _enqueue_intent=lambda intent: enqueued.append(intent),
        _maybe_arm_protection=Mock(),
        _maybe_post_resting_take_profit=AsyncMock(),
        _maybe_emit_protection_exit=protection,
        _refresh_order_metadata_capability=Mock(),
        _maybe_refresh_active_order_metadata=Mock(),
        selected_instrument=SimpleNamespace(instrument_id=InstrumentId("yes")),
        stop_requested=False,
        time_authority=SimpleNamespace(now_corrected_utc=lambda: NOW),
        _current_entry_tau_s=lambda: tau,
    )
    market_runtime = SimpleNamespace(
        zgap=SimpleNamespace(strategy_id=StrategyId("z_gap")),
        prepare_aligned_eval=lambda: SimpleNamespace(snapshot=None),
    )
    return runtime, market_runtime, enqueued, protection


@pytest.mark.asyncio
async def test_strategy_window_opens_only_at_or_above_soft_cutoff() -> None:
    above, market, _enqueued, _protection = _window_runtime(tau=106.0, exposed=False)
    await TradingRuntime.on_async_tick(above, market)
    assert above.capabilities.entry_window_open is True

    below, market, _enqueued, _protection = _window_runtime(tau=105.999, exposed=False)
    await TradingRuntime.on_async_tick(below, market)
    assert below.capabilities.entry_window_open is False


@pytest.mark.asyncio
async def test_between_soft_and_hard_strategy_window_is_closed() -> None:
    runtime, market, enqueued, protection = _window_runtime(tau=105.5, exposed=True)
    await TradingRuntime.on_async_tick(runtime, market)
    assert runtime.capabilities.entry_window_open is False
    assert enqueued == []
    protection.assert_called_once()


@pytest.mark.asyncio
async def test_mandatory_flatten_and_protection_ignore_entry_cutoffs() -> None:
    runtime, market, enqueued, protection = _window_runtime(tau=90.0, exposed=True)
    await TradingRuntime.on_async_tick(runtime, market)
    assert runtime.capabilities.entry_window_open is False
    assert [intent.reason_code for intent in enqueued] == ["MANDATORY_FLATTEN"]
    protection.assert_not_called()

    still_protected, market, enqueued, protection = _window_runtime(tau=100.0, exposed=True)
    await TradingRuntime.on_async_tick(still_protected, market)
    assert still_protected.capabilities.entry_window_open is False
    assert enqueued == []
    protection.assert_called_once()


async def _consume_entry(
    tau: float,
    config_path: str = "config/runs/ask70_protection_tiny_live.yaml",
) -> tuple[list, list, AsyncMock]:
    recorded: list[tuple[str, dict]] = []
    opened: list[dict] = []
    config = load_trading_run_config(Path(config_path))
    intent = _entry()
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(intent)
    token_state = SimpleNamespace(balance_shares=Decimal("0"), allowance_shares=Decimal("0"))
    account = SimpleNamespace(
        status=AccountSnapshotStatus.READY,
        blockers=(),
        token=lambda _token_id: token_state,
        open_order_ids=(),
    )

    class _Hold:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: object) -> bool:
            return False

    async def _open(**kwargs: object) -> None:
        opened.append(kwargs)

    submit_entry = AsyncMock(
        return_value=SimpleNamespace(
            sellable_shares=Decimal("0"),
            phase=ExecutionPhase.COMPLETED_NO_FILL,
            terminal=True,
            working_maker_entry=False,
        )
    )
    runtime = bare_runtime()
    runtime.config = config
    runtime.intent_queue = queue
    runtime.intent_candidate_monotonic_ns = {}
    runtime.run_recorder = SimpleNamespace(
        record=lambda event, payload: recorded.append((event, payload))
    )
    runtime._record_runtime_error = Mock()
    def _select_token(_intent: object) -> tuple[str, SimpleNamespace]:
        return "tok", SimpleNamespace(instrument_id=getattr(_intent, "instrument_id"))

    runtime._select_token = _select_token
    runtime.active_market = SimpleNamespace(
        window_id="window",
        market=SimpleNamespace(condition_id="cond", market_id=SimpleNamespace(value="market")),
        entry_enabled=True,
    )
    runtime.stream_session_id = "stream"
    runtime.account_state_authority = SimpleNamespace(
        current=lambda _mid: account,
        hold_ready_snapshot=lambda _mid: _Hold(),
    )
    runtime._apply_account_snapshot = Mock()
    runtime._current_entry_tau_s = lambda: tau
    runtime.lifecycle = SimpleNamespace(
        open=AsyncMock(side_effect=_open),
        submit_entry=submit_entry,
    )
    runtime.account_state = None
    runtime.selected_token_id = None
    runtime.selected_instrument = None
    runtime.capabilities = SimpleNamespace(selected_token_sellable=False)
    runtime.stop_requested = False
    runtime._maybe_arm_protection = Mock()
    runtime._maybe_post_resting_take_profit = AsyncMock()
    await TradingRuntime._consume_intent(runtime)
    runtime._record_runtime_error.assert_not_called()
    return recorded, opened, submit_entry


@pytest.mark.asyncio
async def test_tau_above_soft_cutoff_consumes_entry() -> None:
    recorded, opened, submit_entry = await _consume_entry(107.0)
    assert [r for r in recorded if r[0] != "ENTRY_OUTCOME"] == []
    outcome = next(payload for event, payload in recorded if event == "ENTRY_OUTCOME")
    assert outcome["kind"] == "ZERO_FILL" and outcome["retry_class"] == "RETRY_AFTER_ZERO_FILL"
    assert len(opened) == 1
    submit_entry.assert_awaited()


@pytest.mark.asyncio
async def test_tau_equal_to_hard_cutoff_still_consumes_entry() -> None:
    _recorded, opened, submit_entry = await _consume_entry(105.0)
    assert len(opened) == 1
    submit_entry.assert_awaited()


@pytest.mark.asyncio
async def test_tau_between_soft_and_hard_still_consumes_queued_entry() -> None:
    _recorded, opened, submit_entry = await _consume_entry(105.5)
    assert len(opened) == 1
    submit_entry.assert_awaited()


@pytest.mark.asyncio
async def test_observed_cutoff_tau_is_rejected_before_session_or_mutation() -> None:
    recorded, opened, submit_entry = await _consume_entry(104.930171)
    assert opened == []
    submit_entry.assert_not_awaited()
    outcomes = [payload for event, payload in recorded if event == "ENTRY_OUTCOME"]
    assert [(o["kind"], o["reason"], o["retry_class"]) for o in outcomes] == [
        ("NOT_SENT", "ENTRY_CUTOFF_PASSED", "END_EPISODE")
    ]
    recorded = [r for r in recorded if r[0] != "ENTRY_OUTCOME"]
    assert recorded == [
        (
            "INTENT_BLOCKED",
            {
                "intent_id": recorded[0][1]["intent_id"],
                "intent_type": "EnterIntent",
                "reason": "ENTRY_CUTOFF_PASSED",
                "tau_s": 104.930171,
                "cutoff_s": 105.0,
            },
        )
    ]


@pytest.mark.asyncio
async def test_experimental_cutoff_rejects_before_session_or_mutation() -> None:
    path = "config/runs/ask70_protection_tau30_experimental.yaml"
    recorded, opened, submit_entry = await _consume_entry(29.5, path)
    assert opened == []
    submit_entry.assert_not_awaited()
    assert recorded[0][0] == "INTENT_BLOCKED"
    assert recorded[0][1]["reason"] == "ENTRY_CUTOFF_PASSED"
    assert recorded[0][1]["cutoff_s"] == 30.0
    assert recorded[0][1]["tau_s"] == 29.5


@pytest.mark.asyncio
async def test_experimental_cutoff_still_consumes_tau_at_the_hard_floor() -> None:
    path = "config/runs/ask70_protection_tau30_experimental.yaml"
    _recorded, opened, submit_entry = await _consume_entry(30.0, path)
    assert len(opened) == 1
    submit_entry.assert_awaited()


def _evidence(event_type: str, payload: dict, sequence: int) -> RunEvidenceRecord:
    return RunEvidenceRecord(
        sequence=sequence,
        event_id=f"e{sequence}",
        run_instance_id="run",
        event_type=event_type,
        observed_at=NOW,
        payload=payload,
    )


def _cutoff_records() -> tuple[RunEvidenceRecord, ...]:
    return (
        _evidence(
            "INTENT_EMITTED",
            {"intent_id": "entry-1", "intent_type": "EnterIntent"},
            1,
        ),
        _evidence(
            "INTENT_BLOCKED",
            {
                "intent_id": "entry-1",
                "intent_type": "EnterIntent",
                "reason": "ENTRY_CUTOFF_PASSED",
                "tau_s": 104.930171,
                "cutoff_s": 105.0,
            },
            2,
        ),
    )


def _report(records: tuple[RunEvidenceRecord, ...], **overrides: object) -> dict:
    fields: dict = {
        "run_instance_id": "run",
        "configured_run_name": "ask70_protection_tiny_live",
        "live_requested": True,
        "state": None,
        "mutation_attempts": 0,
        "process_mutation_attempts": 0,
        "execution_timeline": (),
        "capabilities": {},
        "market_data": None,
        "run_evidence": records,
        "runtime_errors": (),
        "strategy_started": True,
    }
    fields.update(overrides)
    return build_run_report(RunReportInput(**fields))


def test_cutoff_only_no_entry_is_a_safe_incomplete_validation() -> None:
    payload = _report(_cutoff_records(), process_mutation_attempts=4)
    assert payload["outcome"] == "NO_ENTRY_CUTOFF_BLOCKED"
    assert payload["ok"] is True
    assert payload["mutation_attempts"] == 0
    assert payload["process_mutation_attempts"] == 4
    assert payload["strategy_session"] is None
    assert payload["validation_scope"]["execution_lifecycle_exercised"] is False
    assert payload["validation_scope"]["venue_mutation_attempted"] is False
    block = payload["entry_cutoff_block"]
    assert block["intent_id"] == "entry-1"
    assert block["tau_s"] == pytest.approx(104.930171)
    assert block["cutoff_s"] == 105.0
    assert block["missed_by_s"] == pytest.approx(0.069829)
    assert block["reason"] == "ENTRY_CUTOFF_PASSED"
    note = payload["validation_scope"]["note"]
    assert "Execution lifecycle was not exercised" in note
    assert "No BUY, fill, protection, or exit was tested" in note
    assert "R7 confirmed-entry/exit acceptance remains incomplete" in note
    assert "entry-1" in note
    assert "104.930171" in note
    assert "105.0" in note
    assert "Mutation attempts remain zero" in note
    assert "does not satisfy" in note


@pytest.mark.asyncio
async def test_cutoff_only_runtime_writes_safe_no_entry_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = await _reporting_runtime(tmp_path)
    runtime.run_recorder.record(
        "INTENT_EMITTED",
        {"intent_id": "entry-1", "intent_type": "EnterIntent"},
    )
    runtime.run_recorder.record(
        "INTENT_BLOCKED",
        {
            "intent_id": "entry-1",
            "intent_type": "EnterIntent",
            "reason": "ENTRY_CUTOFF_PASSED",
            "tau_s": 104.930171,
            "cutoff_s": 105.0,
        },
    )
    monkeypatch.setattr(trading_runtime_module, "run_market_data_runtime", _completed_market_data)
    result = await runtime.run()
    payload = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert result.outcome == "NO_ENTRY_CUTOFF_BLOCKED"
    assert result.ok is True
    assert payload["mutation_attempts"] == 0
    assert payload["strategy_session"] is None
    assert payload["validation_scope"]["execution_lifecycle_exercised"] is False
    assert payload["entry_cutoff_block"]["reason"] == "ENTRY_CUTOFF_PASSED"
    note = payload["validation_scope"]["note"]
    assert "R7 confirmed-entry/exit acceptance remains incomplete" in note


def test_more_serious_failures_are_not_relabeled_as_cutoff() -> None:
    records = _cutoff_records()
    startup = _report(records, startup_outcome="STARTUP_RECONCILIATION_BLOCKED")
    assert startup["outcome"] == "STARTUP_RECONCILIATION_BLOCKED"
    assert startup["ok"] is False
    assert startup["entry_cutoff_block"] is None

    fatal = _report(records, fatal_error={"stage": "interrupt", "error_type": "KeyboardInterrupt"})
    assert fatal["outcome"] == "RUNTIME_FAILURE"
    assert fatal["ok"] is False

    degraded = _report(records, runtime_errors=("intent:RuntimeError:prepare failed",))
    assert degraded["outcome"] == "RUNTIME_DEGRADED"

    reporting = _report(records, reporting_failures=("mutation_projection:OSError:locked",))
    assert reporting["outcome"] == "REPORTING_DEGRADED"

    posted = _report(
        records,
        state=ExecutionSessionState(phase=ExecutionPhase.COMPLETED_NO_DISPATCH),
        mutation_attempts=0,
    )
    assert posted["outcome"] == "COMPLETED_NO_DISPATCH"
    assert posted["entry_cutoff_block"] is None

    account = _report(
        (
            _evidence("INTENT_EMITTED", {"intent_id": "entry-1", "intent_type": "EnterIntent"}, 1),
            _evidence(
                "INTENT_BLOCKED",
                {"intent_id": "entry-1", "reason": "ACCOUNT_STATE_NOT_AUTHORITATIVE"},
                2,
            ),
        )
    )
    assert account["outcome"] == "ENTRY_ABORTED_BEFORE_SESSION"
    assert account["ok"] is False

    mixed = _report(
        _cutoff_records()
        + (
            _evidence(
                "INTENT_BLOCKED",
                {"intent_id": "entry-1", "reason": "ACCOUNT_STATE_NOT_AUTHORITATIVE"},
                3,
            ),
        )
    )
    assert mixed["outcome"] == "ENTRY_ABORTED_BEFORE_SESSION"

    unknown = _report(_cutoff_records(), mutation_attempts=None)
    assert unknown["outcome"] == "ENTRY_ABORTED_BEFORE_SESSION"


def test_entry_dispatch_margin_must_be_finite_and_non_negative(tmp_path: Path) -> None:
    from tyrex_pm.runtime.run_config import RunConfigError, strategy_entry_cutoff_s

    text = Path("config/runs/z_gap_tiny_live.yaml").read_text(encoding="utf-8")
    zero = tmp_path / "zero.yaml"
    zero.write_text(
        text.replace(
            "evaluation_interval_s: 1\n",
            "evaluation_interval_s: 1\n  entry_dispatch_margin_s: 0\n",
        ),
        encoding="utf-8",
    )
    loaded = load_trading_run_config(zero)
    assert loaded.market.entry_dispatch_margin_s == 0.0
    assert strategy_entry_cutoff_s(loaded) == 105.0

    for raw, match in (
        ("-1", "finite and >= 0"),
        ("true", "finite number"),
        (".nan", "finite and >= 0"),
        (".inf", "finite and >= 0"),
        ("200", "strategy-facing entry cutoff"),
    ):
        path = tmp_path / f"bad-{raw}.yaml"
        path.write_text(
            text.replace(
                "evaluation_interval_s: 1\n",
                f"evaluation_interval_s: 1\n  entry_dispatch_margin_s: {raw}\n",
            ),
            encoding="utf-8",
        )
        with pytest.raises(RunConfigError, match=match):
            load_trading_run_config(path)
