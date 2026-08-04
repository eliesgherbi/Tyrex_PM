"""Fake LIVE pre-mutation barrier and CRITICAL_AUDIT_FAILURE behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.commands import (
    CancelOrderCommand,
    ExecutionPolicy,
    SubmitOrderCommand,
    new_client_order_id,
    new_command_id,
)
from tyrex_pm.core.ids import (
    InstrumentId,
    MarketId,
    StrategyId,
    new_correlation_id,
)
from tyrex_pm.core.intents import IntentId, OrderSide
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.execution.fill_ledger import FillLedger
from tyrex_pm.execution.order_store import OrderStore
from tyrex_pm.execution.polymarket.fake_transport import FakeTransport
from tyrex_pm.execution.polymarket.live_oms import LiveOMS, SubmissionState
from tyrex_pm.planning.plan import PlanId
from tyrex_pm.portfolio.portfolio import Portfolio
from tyrex_pm.reporting import ReportingHealth, open_run_reporter

T0 = datetime(2026, 7, 16, 20, 0, 0, tzinfo=timezone.utc)
TOKEN = InstrumentId("tok-yes-1")
MARKET = MarketId("m1")


def _cmd(*, side: OrderSide = OrderSide.BUY) -> SubmitOrderCommand:
    return SubmitOrderCommand(
        command_id=new_command_id(),
        plan_id=PlanId("p1"),
        intent_id=IntentId("i1"),
        strategy_id=StrategyId("z_gap"),
        instrument_id=TOKEN,
        market_id=MARKET,
        side=side,
        quantity=Decimal("5"),
        limit_price=Decimal("0.52"),
        client_order_id=new_client_order_id(),
        created_at=T0,
        correlation_id=new_correlation_id(),
        causation_id=None,
        execution_policy=ExecutionPolicy.NORMAL,
    )


def _oms(tmp_path: Path, *, fail_critical: bool = False):
    disp = EventDispatcher()
    orders = OrderStore()
    ledger = FillLedger()
    portfolio = Portfolio(fill_ledger=ledger, market_id=MARKET)
    orders.attach(disp)
    ledger.attach(disp)
    portfolio.attach(disp)
    transport = FakeTransport()
    rep = open_run_reporter(
        run_dir=tmp_path / "live_run",
        run_id="fake_live_barrier",
        mode="live",
        strategy_id="z_gap",
        performance_label="real",
        fake_transport=True,
        identity_extra={"transport": "FakeTransport"},
    )
    if fail_critical:
        assert rep._writer is not None
        rep._writer.fail_critical_writes = True
    oms = LiveOMS(
        transport=transport,
        dispatcher=disp,
        order_store=orders,
        portfolio=portfolio,
        mutations_enabled=True,
        reporter=rep,
    )
    return oms, transport, rep


def test_pre_mutation_ack_ordering_blocks_failed_persist(tmp_path: Path):
    oms, transport, rep = _oms(tmp_path, fail_critical=True)
    before = len(transport.submitted)
    oid = oms.submit(_cmd())
    track = oms._tracking[oid.value]
    assert track.submission is SubmissionState.REJECTED
    assert len(transport.submitted) == before
    assert rep.health is ReportingHealth.CRITICAL_AUDIT_FAILURE
    assert not rep.allows_new_exposure
    rep.finalize(terminal_status="ABORTED", terminal_reason="barrier", clean_shutdown=False)


def test_critical_failure_blocks_entry_allows_cancel_and_sell(tmp_path: Path):
    oms, transport, rep = _oms(tmp_path)
    oid1 = oms.submit(_cmd())
    assert oms._tracking[oid1.value].submission is SubmissionState.VENUE_ACCEPTED

    rep.force_critical_failure_for_tests()
    assert not rep.allows_new_exposure

    before = len(transport.submitted)
    oid2 = oms.submit(_cmd())
    assert oms._tracking[oid2.value].submission is SubmissionState.REJECTED
    assert len(transport.submitted) == before

    sell_before = len(transport.submitted)
    oid3 = oms.submit(_cmd(side=OrderSide.SELL))
    assert len(transport.submitted) == sell_before + 1
    assert oms._tracking[oid3.value].submission is SubmissionState.VENUE_ACCEPTED

    cancel_before = len(transport.cancelled)
    oms.cancel(
        CancelOrderCommand(
            command_id=new_command_id(),
            order_id=oid1,
            reason_code="TEST",
            created_at=T0,
            correlation_id=new_correlation_id(),
            causation_id=None,
        )
    )
    assert len(transport.cancelled) == cancel_before + 1
    oms.run_reconciliation(market_id="m1")
    rep.finalize(terminal_status="ABORTED", terminal_reason="critical", clean_shutdown=False)


def test_fake_live_artifact_shape(tmp_path: Path):
    from tyrex_pm.runtime.n7_operator_run import run_fake_oneshot_no_signal

    cfg = Path("config/n7_tiny_live.json")
    if not cfg.is_file():
        pytest.skip("n7 sealed config missing")
    result = run_fake_oneshot_no_signal(out_dir=tmp_path / "n7_fake", config_path=cfg)
    run_dir = tmp_path / "n7_fake"
    assert (run_dir / "manifest.json").is_file()
    assert (run_dir / "run_summary.json").is_file()
    assert (run_dir / "audit_events.jsonl").is_file()
    assert (run_dir / "analytics_events.jsonl").is_file()
    assert result.report_path.name == "run_summary.json"
    assert not (run_dir / "n7_oneshot_report.json").exists()
    manifest = (run_dir / "manifest.json").read_text(encoding="utf-8")
    assert "FakeTransport" in manifest or "fake_transport" in manifest
