"""Guided recovery, startup gating, and process exclusion."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tyrex_pm.application.startup_interaction import request_decision
from tyrex_pm.core.ids import CorrelationId, InstrumentId, MarketId, StrategyId
from tyrex_pm.core.intents import FlattenIntent, new_intent_id
from tyrex_pm.execution.coordinator import (
    AccountExecutionCoordinator,
    DispatchBlocked,
    GatewaySubmissionResult,
    PreparedOrder,
)
from tyrex_pm.execution.evidence import (
    ManualInterventionRequired,
    ReconciliationObserved,
    new_envelope,
)
from tyrex_pm.execution.final_gate import FinalExecutionGate
from tyrex_pm.execution.lifecycle import ExecutionLifecycle, LifecyclePolicy
from tyrex_pm.execution.planner import ExecutionRiskPolicy, IntentOrderPlanner
from tyrex_pm.execution.process_lock import StateDirectoryBusy, StateDirectoryLock
from tyrex_pm.execution.reconciliation import SessionReconciler
from tyrex_pm.execution.startup_reconciliation import RecoveryProposal
from tyrex_pm.market_data.book_health import SyncHealth
from tyrex_pm.persistence.execution_journal import MemoryExecutionJournal
from tyrex_pm.reporting.startup_report import startup_report_payload
from tyrex_pm.runtime.capabilities import CapabilityController
from tyrex_pm.runtime.startup_controller import StartupController

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


class _Venue:
    def __init__(self) -> None:
        self.posts: list[str] = []
        self.balance = Decimal("7")
        self.open_ids: tuple[str, ...] = ()
        self.trades: list[SimpleNamespace] = []

    async def prepare_order(self, spec):  # noqa: ANN001
        return PreparedOrder(spec.order_id, spec.order_id, spec)

    async def post_order(self, prepared):  # noqa: ANN001
        self.posts.append(prepared.payload.side.value)
        self.balance = Decimal("0")
        return GatewaySubmissionResult(
            accepted=True,
            venue_order_id="venue-exit",
            status="matched",
            cumulative_matched_shares=prepared.payload.shares,
            trade_ids=("trade-exit",),
        )

    async def discard_prepared(self, _prepared) -> None:
        return None

    async def cancel_orders(self, order_ids):  # noqa: ANN001
        from tyrex_pm.execution.cancel import cancel_result_from_sdk

        self.open_ids = ()
        return cancel_result_from_sdk({"canceled": list(order_ids), "not_canceled": {}})

    async def list_open_orders(self, **_kwargs):
        return tuple({"id": item} for item in self.open_ids)

    async def list_account_trades(self, **_kwargs):
        return tuple(self.trades)

    async def conditional_balance(self, _token_id):
        return self.balance, Decimal("100")

    async def close(self) -> None:
        return None


def _view():
    leg = SimpleNamespace(
        token_id="token",
        sync_health=SyncHealth.READY,
        data_age_ms=10,
        tick_size=Decimal("0.01"),
        quote=SimpleNamespace(
            best_ask=Decimal("0.50"),
            best_bid=Decimal("0.40"),
            ask_size_at_touch=Decimal("100"),
        ),
    )
    return SimpleNamespace(binding_id="old-binding", up=leg, down=SimpleNamespace(token_id="no"))


def _caps() -> CapabilityController:
    return CapabilityController(
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


def _lifecycle(venue: _Venue) -> ExecutionLifecycle:
    gate = FinalExecutionGate(
        capture_book=_view,
        capabilities=_caps(),
        active_binding_id=lambda: "old-binding",
    )
    coordinator = AccountExecutionCoordinator(
        journal=MemoryExecutionJournal(),
        gateway=venue,
        final_gate=gate,
        recovery_generation="gen",
    )
    return ExecutionLifecycle(
        coordinator=coordinator,
        reconciler=SessionReconciler(venue),
        planner=IntentOrderPlanner(ExecutionRiskPolicy(maximum_total_debit=Decimal("5"))),
        capabilities=_caps(),
        policy=LifecyclePolicy(
            evidence_timeout_s=1,
            reconciliation_interval_s=0.001,
            exit_retry_limit=2,
            exit_retry_budget_s=1,
            cancel_delay_s=0.001,
            sell_size_increment=Decimal("0.01"),
        ),
    )


async def _manual(lifecycle: ExecutionLifecycle) -> None:
    await lifecycle.open(
        session_id="old",
        strategy_id="ask70",
        market_id="old-market",
        window_id="old-window",
        token_id="token",
        baseline_position_shares=Decimal("0"),
        baseline_sellable_shares=Decimal("0"),
    )
    await lifecycle.coordinator.apply(
        ReconciliationObserved(
            **new_envelope(session_id="old", dedupe_key="pos"),
            source="test",
            complete=True,
            confirmed_position_shares=Decimal("7"),
            sellable_shares=Decimal("7"),
            open_order_ids=(),
        )
    )
    await lifecycle.coordinator.apply(
        ManualInterventionRequired(
            **new_envelope(session_id="old", dedupe_key="manual"),
            reason="restored",
        )
    )


def _proposal(**overrides) -> RecoveryProposal:
    values = dict(
        issue_id="close:old",
        session_id="old",
        condition_id="old-market",
        token_id="token",
        action="cancel_and_close",
        max_shares=Decimal("7"),
        min_price=Decimal("0.01"),
    )
    values.update(overrides)
    return RecoveryProposal(**values)


@pytest.mark.asyncio
async def test_s15_approved_close_uses_the_real_gate_and_old_market() -> None:
    venue = _Venue()
    lifecycle = _lifecycle(venue)
    await _manual(lifecycle)
    controller = StartupController(
        coordinator=lifecycle.coordinator, now=lambda: NOW, generation="gen"
    )
    proposal = _proposal()
    assert await controller.authorize(proposal, approved=True, live=True)
    lifecycle.coordinator.recovery_now = lambda: NOW
    intent = FlattenIntent(
        intent_id=new_intent_id(),
        strategy_id=StrategyId("ask70"),
        instrument_id=InstrumentId("token"),
        market_id=MarketId("old-market"),
        created_at=NOW,
        correlation_id=CorrelationId("recovery"),
        causation_id=None,
        reason_code="APPROVED_RECOVERY",
    )
    await lifecycle.submit_exit(
        intent,
        token_id="token",
        min_order_size=Decimal("1"),
        last_bid=Decimal("0.40"),
    )
    assert venue.posts == ["SELL"]
    assert lifecycle.coordinator.final_gate.last_view.binding_id == "old-binding"
    await lifecycle.coordinator.close()


@pytest.mark.asyncio
async def test_s16_quantity_above_ceiling_does_not_post() -> None:
    venue = _Venue()
    lifecycle = _lifecycle(venue)
    await _manual(lifecycle)
    controller = StartupController(
        coordinator=lifecycle.coordinator, now=lambda: NOW, generation="gen"
    )
    await controller.authorize(_proposal(max_shares=Decimal("1")), approved=True, live=True)
    lifecycle.coordinator.recovery_now = lambda: NOW
    intent = FlattenIntent(
        intent_id=new_intent_id(),
        strategy_id=StrategyId("ask70"),
        instrument_id=InstrumentId("token"),
        market_id=MarketId("old-market"),
        created_at=NOW,
        correlation_id=CorrelationId("recovery"),
        causation_id=None,
        reason_code="APPROVED_RECOVERY",
    )
    with pytest.raises(DispatchBlocked, match="quantity_above_ceiling"):
        await lifecycle.submit_exit(
            intent, token_id="token", min_order_size=Decimal("1"), last_bid=Decimal("0.40")
        )
    assert venue.posts == []
    await lifecycle.coordinator.close()


@pytest.mark.asyncio
async def test_s18_timeout_and_non_tty_do_not_approve() -> None:
    assessment = SimpleNamespace(
        proposals=(_proposal(),),
        issues=(),
        summary="needs a choice",
        wallet_display="",
    )

    async def _slow(_prompt: str):
        await asyncio.sleep(1)
        return "approve"

    decision = await request_decision(
        assessment, live=True, reader=_slow, timeout_s=0.01
    )
    assert decision.approved is False
    silent = await request_decision(assessment, live=True, reader=None, timeout_s=1)
    assert silent.reason == "no_terminal"
    paper = await request_decision(assessment, live=False, reader=_slow, timeout_s=1)
    assert paper.reason == "live_not_requested"


def test_s23_second_process_cannot_lock_the_same_directory(tmp_path) -> None:
    first = StateDirectoryLock(tmp_path)
    second = StateDirectoryLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(StateDirectoryBusy):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
    assert (tmp_path / ".tyrex_state.lock").exists()


def test_s24_expired_assessment_blocks_buy_and_keeps_exit() -> None:
    caps = _caps()
    caps.set_blocker("STARTUP_ASSESSMENT_EXPIRED", "stale")
    entry = caps.snapshot(exposed=False)
    exit_caps = caps.snapshot(exposed=True)
    assert entry.entry_executable is False
    assert "STARTUP_ASSESSMENT_EXPIRED" in entry.blockers
    assert exit_caps.exit_executable is True


def test_startup_report_names_the_blocker_without_authorizing() -> None:
    from tyrex_pm.execution.startup_reconciliation import (
        AccountIdentity,
        InspectionFacts,
        ReadCoverage,
        assess,
    )

    assessment = assess(
        InspectionFacts(
            identity=AccountIdentity(
                chain_id=137,
                environment="production",
                wallet="0xabcdef1234567890",
            ),
            coverage=ReadCoverage(
                complete=False,
                positions_complete=False,
                orders_complete=False,
                activity_complete=False,
                reason="transport stalled",
            ),
        )
    )
    payload = startup_report_payload(assessment, run_name="ask70", live_requested=False)
    assert payload["status"] == "BLOCKED"
    assert payload["mutation_attempts"] == 0
    assert "0xabcd" in payload["wallet_display"]
    assert "abcdef1234567890" not in payload["wallet_display"]


def test_s19_failed_read_is_not_a_zero_balance() -> None:
    from tyrex_pm.adapters.polymarket.startup_reads import coverage_from_pages

    coverage = coverage_from_pages(pages_read=1, exhausted=False, below_threshold_included=True)
    assert coverage.complete is False
    assert coverage.reason == "pagination_truncated"


@pytest.mark.asyncio
async def test_s21_blocked_startup_does_not_start_feeds(tmp_path, monkeypatch) -> None:
    from pathlib import Path

    from tyrex_pm.execution.startup_reconciliation import (
        AccountIdentity,
        InspectionFacts,
        ReadCoverage,
    )
    from tyrex_pm.runtime import trading_runtime as trading_runtime_module
    from tyrex_pm.runtime.run_config import load_trading_run_config
    from tyrex_pm.runtime.trading_runtime import TradingRuntime

    called = {"value": False}

    async def _feeds(**_kwargs):
        called["value"] = True
        raise AssertionError("market data started")

    monkeypatch.setattr(trading_runtime_module, "run_market_data_runtime", _feeds)
    from dataclasses import replace

    config = replace(
        load_trading_run_config(Path("config/runs/ask70_protection_tiny_live.yaml")),
        state_directory=tmp_path / "state",
        report_directory=tmp_path / "reports",
    )

    class _Reader:
        mutations_enabled = True

        def startup_inspection(self):
            return InspectionFacts(
                identity=AccountIdentity(
                    chain_id=137, environment="production", wallet="0xabc"
                ),
                coverage=ReadCoverage(
                    complete=False,
                    positions_complete=False,
                    orders_complete=False,
                    activity_complete=False,
                    reason="visibility incomplete",
                ),
            )

        async def close(self) -> None:
            return None

        async def stop_user_stream(self) -> None:
            return None

    runtime = await TradingRuntime.create(
        config=config,
        output_directory=tmp_path / "out",
        env={},
        gateway=_Reader(),
    )
    result = await runtime.run()
    assert called["value"] is False
    assert result.outcome == "STARTUP_RECONCILIATION_BLOCKED"


def test_check_account_rejects_live_flag() -> None:
    from tyrex_pm.application.cli import main

    code = main(
        [
            "run",
            "--config",
            "config/runs/z_gap_tiny_live.yaml",
            "--check-account",
            "--live",
        ]
    )
    assert code == 2


@pytest.mark.asyncio
async def test_s01_inspection_gateway_cannot_post() -> None:
    from tyrex_pm.execution.coordinator import PreparedOrder
    from tyrex_pm.execution.polymarket.gateway import PolymarketAsyncGateway

    gateway = PolymarketAsyncGateway(client=object())
    gateway.mutations_enabled = False
    with pytest.raises(RuntimeError, match="cannot post"):
        await gateway.post_order(PreparedOrder("id", "digest", object()))


def test_historical_lookup_uses_condition_ids_not_gamma_id() -> None:
    from tyrex_pm.adapters.polymarket.startup_reads import (
        SDK_FINALITY_LIMITATION,
        historical_market_query,
        lifecycle_from_sdk_market,
        position_list_kwargs,
    )

    assert historical_market_query(condition_id="0xcond") == {"condition_ids": ("0xcond",)}
    assert position_list_kwargs()["size_threshold"] == 0.0
    closed = lifecycle_from_sdk_market({"closed": True, "uma_resolution_status": "resolved"})
    assert closed["status"] == "CLOSED_PENDING_RESOLUTION"
    assert closed["proof_supported"] == "false"
    tradable = lifecycle_from_sdk_market({"closed": False, "accepting_orders": True})
    assert tradable["status"] == "TRADABLE"
    assert tradable["proof_supported"] == "false"
    unknown = lifecycle_from_sdk_market({})
    assert unknown["status"] == "UNKNOWN"
    assert "0.5.0" in SDK_FINALITY_LIMITATION
