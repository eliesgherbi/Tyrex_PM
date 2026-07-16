"""R6 reconciliation classifications."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.commands import (
    ExecutionPolicy,
    SubmitOrderCommand,
    new_client_order_id,
    new_command_id,
)
from tyrex_pm.core.ids import InstrumentId, MarketId, StrategyId, new_correlation_id, new_order_id
from tyrex_pm.core.intents import IntentId, OrderSide
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.execution.fill_ledger import FillLedger
from tyrex_pm.execution.order_store import OrderStore
from tyrex_pm.execution.polymarket.fake_transport import FakeTransport
from tyrex_pm.execution.polymarket.live_oms import LiveOMS
from tyrex_pm.execution.polymarket.reconciliation import ReconcileClass, ReconciliationService
from tyrex_pm.execution.polymarket.transport import VenueOrderSnapshot, VenuePositionSnapshot, VenueTradeSnapshot
from tyrex_pm.planning.plan import PlanId
from tyrex_pm.portfolio.portfolio import Portfolio

T0 = datetime(2026, 7, 16, 20, 0, 0, tzinfo=timezone.utc)
TOKEN = InstrumentId("tok-yes-1")
MARKET = MarketId("m1")


def _portfolio_stack():
    disp = EventDispatcher()
    orders = OrderStore()
    ledger = FillLedger()
    portfolio = Portfolio(fill_ledger=ledger, market_id=MARKET)
    orders.attach(disp)
    ledger.attach(disp)
    portfolio.attach(disp)
    return disp, orders, ledger, portfolio


def test_unknown_external_order_never_auto_canceled() -> None:
    _, orders, _, portfolio = _portfolio_stack()
    recon = ReconciliationService(order_store=orders, portfolio=portfolio)
    report = recon.reconcile(
        venue_orders=[
            VenueOrderSnapshot(
                venue_order_id="0xexternal",
                status="live",
                instrument_token_id="tok-yes-1",
                side="BUY",
                original_size=Decimal("1"),
                size_matched=Decimal("0"),
                price=Decimal("0.5"),
            )
        ],
        venue_trades=[],
        venue_positions=[],
    )
    assert any(f.classification is ReconcileClass.UNKNOWN_EXTERNAL_ORDER for f in report.findings)
    assert report.blocks_entry
    assert report.requires_manual


def test_position_mismatch_blocks_entry() -> None:
    _, orders, _, portfolio = _portfolio_stack()
    recon = ReconciliationService(order_store=orders, portfolio=portfolio)
    report = recon.reconcile(
        venue_orders=[],
        venue_trades=[],
        venue_positions=[
            VenuePositionSnapshot(
                instrument_token_id="tok-yes-1",
                size=Decimal("10"),
            )
        ],
    )
    assert any(f.classification is ReconcileClass.POSITION_MISMATCH for f in report.findings)
    assert report.blocks_entry


def test_missing_evidence_never_flat() -> None:
    _, orders, _, portfolio = _portfolio_stack()
    recon = ReconciliationService(order_store=orders, portfolio=portfolio)
    report = recon.reconcile(
        venue_orders=[],
        venue_trades=[],
        venue_positions=[],
        missing_evidence=True,
    )
    assert any(f.classification is ReconcileClass.UNRESOLVED for f in report.findings)
    assert report.blocks_entry


def test_fill_missing_local_idempotent_mark() -> None:
    _, orders, _, portfolio = _portfolio_stack()
    recon = ReconciliationService(order_store=orders, portfolio=portfolio)
    recon.register_owned("0xown")
    trade = VenueTradeSnapshot(
        venue_trade_id="trade-1",
        venue_order_id="0xown",
        instrument_token_id="tok-yes-1",
        side="BUY",
        size=Decimal("1"),
        price=Decimal("0.5"),
        status="CONFIRMED",
    )
    r1 = recon.reconcile(venue_orders=[], venue_trades=[trade], venue_positions=[])
    assert any(f.classification is ReconcileClass.FILL_MISSING_LOCAL for f in r1.findings)
    recon.mark_trade_applied("trade-1")
    r2 = recon.reconcile(venue_orders=[], venue_trades=[trade], venue_positions=[])
    assert not any(f.classification is ReconcileClass.FILL_MISSING_LOCAL for f in r2.findings)


def test_local_working_absent_from_venue() -> None:
    disp, orders, _, portfolio = _portfolio_stack()
    transport = FakeTransport()
    oms = LiveOMS(
        transport=transport,
        dispatcher=disp,
        order_store=orders,
        portfolio=portfolio,
        mutations_enabled=True,
    )
    cmd = SubmitOrderCommand(
        command_id=new_command_id(),
        plan_id=PlanId("p"),
        intent_id=IntentId("i"),
        strategy_id=StrategyId("reference_momentum"),
        instrument_id=TOKEN,
        market_id=MARKET,
        side=OrderSide.BUY,
        quantity=Decimal("5"),
        limit_price=Decimal("0.5"),
        client_order_id=new_client_order_id(),
        created_at=T0,
        correlation_id=new_correlation_id(),
        causation_id=None,
        execution_policy=ExecutionPolicy.NORMAL,
    )
    oid = oms.submit(cmd)
    rec = orders.get(oid)
    assert rec and rec.venue_order_id
    # Venue drops the order without local cancel
    transport.open_orders.clear()
    report = oms.run_reconciliation()
    assert any(f.classification is ReconcileClass.VENUE_MISSING for f in report.findings)
    assert report.blocks_entry
