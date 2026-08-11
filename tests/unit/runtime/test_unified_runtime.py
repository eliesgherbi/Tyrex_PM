from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import tyrex_pm.runtime.trading_runtime as trading_runtime_module
from tyrex_pm.application.cli import main
from tyrex_pm.core.ids import CorrelationId, InstrumentId, MarketId, StrategyId
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.intents import EnterIntent, ExitIntent, new_intent_id
from tyrex_pm.execution.coordinator import (
    AccountExecutionCoordinator,
    GatewaySubmissionResult,
    PreparedOrder,
)
from tyrex_pm.execution.lifecycle import ExecutionLifecycle, LifecyclePolicy
from tyrex_pm.execution.planner import ExecutionRiskPolicy, IntentOrderPlanner
from tyrex_pm.execution.reconciliation import SessionReconciler
from tyrex_pm.execution.session_state import ExecutionPhase
from tyrex_pm.persistence.execution_journal import MemoryExecutionJournal
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


def test_single_run_config_is_strict_and_keeps_tiny_live_cap() -> None:
    cfg = load_trading_run_config(Path("config/runs/z_gap_tiny_live.yaml"))
    assert cfg.strategy_kind == "z_gap"
    assert cfg.risk.maximum_total_debit == Decimal("5")
    assert cfg.lifecycle.mandatory_exit_before_end_s > cfg.lifecycle.manual_deadline_before_end_s


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
        prior_scope_clear=True,
        entry_window_open=True,
    )
    result = caps.snapshot()
    assert result.execution_infrastructure_ready
    assert not result.entry_executable
    assert not result.decision_ready
    assert "MODEL_NOT_READY" in result.blockers
    assert "ACCOUNT_READS_NOT_READY" not in result.blockers


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
    runtime = SimpleNamespace(
        _bind_pending_market=AsyncMock(),
        _market_data_ready=lambda: False,
        active_market=None,
        intent_queue=asyncio.Queue(),
        _consume_intent=AsyncMock(),
        lifecycle=SimpleNamespace(state=None),
    )
    runtime.intent_queue.put_nowait(_exit())
    await TradingRuntime.on_async_tick(runtime, SimpleNamespace())
    runtime._consume_intent.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_deadline_is_enforced_with_open_exposure() -> None:
    state = SimpleNamespace(has_exposure=True, exit_requested_reason="MANDATORY_FLATTEN")
    lifecycle = SimpleNamespace(state=state, mark_manual=AsyncMock())
    runtime = SimpleNamespace(
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
            strategy=SimpleNamespace(entry=SimpleNamespace(tau_min_s=0, tau_max_s=300)),
            lifecycle=SimpleNamespace(
                mandatory_exit_before_end_s=90,
                manual_deadline_before_end_s=45,
            ),
        ),
        capabilities=SimpleNamespace(entry_window_open=False),
        lifecycle=lifecycle,
        intent_queue=asyncio.Queue(),
        _consume_intent=AsyncMock(),
        selected_instrument=None,
        stop_requested=False,
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
    state = await lifecycle.submit_exit(_exit(), token_id="token")
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
    assert report["outcome"] == "ENTRY_PRE_DISPATCH_FAILED"
    assert report["ok"] is False
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
    runtime = SimpleNamespace(
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
    assert payload["schema_version"] == 4
    assert payload["mutation_attempts"] == 0
    assert payload["run_evidence_summary"]["strategy_reason_counts"] == {"INSUFFICIENT_EDGE": 1}
    assert payload["reporting"]["execution_authority"] == "sqlite.execution_events"
    assert payload["validation_scope"]["execution_lifecycle_exercised"] is False


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
    monkeypatch.setattr(
        trading_runtime_module,
        "build_run_report",
        lambda _source: (_ for _ in ()).throw(ValueError("projection failed")),
    )
    result = await runtime.run()
    payload = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert result.outcome == "RUNTIME_FAILURE"
    assert payload["reporting"]["emergency"] is True
    assert payload["reporting"]["error_type"] == "ValueError"
    assert payload["mutation_attempts"] == 0
