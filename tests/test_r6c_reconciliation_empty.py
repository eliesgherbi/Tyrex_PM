"""R6C: clean empty account vs unreachable / missing evidence."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.execution.fill_ledger import FillLedger
from tyrex_pm.execution.order_store import OrderStore
from tyrex_pm.execution.polymarket.live_oms import LiveOMS
from tyrex_pm.execution.polymarket.readiness import ReadinessReason
from tyrex_pm.execution.polymarket.reconciliation import ReconcileClass, ReconciliationService
from tyrex_pm.execution.polymarket.fake_transport import FakeTransport
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.portfolio.portfolio import Portfolio


def _stack():
    disp = EventDispatcher()
    orders = OrderStore()
    ledger = FillLedger()
    portfolio = Portfolio(fill_ledger=ledger)
    return disp, orders, ledger, portfolio


def test_clean_empty_account_distinct_from_unreachable() -> None:
    _, orders, _, portfolio = _stack()
    recon = ReconciliationService(order_store=orders, portfolio=portfolio)
    clean = recon.reconcile(
        venue_orders=[],
        venue_trades=[],
        venue_positions=[],
        missing_evidence=False,
    )
    assert clean.findings == []
    assert not clean.blocks_entry

    unreachable = recon.reconcile(
        venue_orders=[],
        venue_trades=[],
        venue_positions=[],
        missing_evidence=True,
    )
    assert any(f.classification is ReconcileClass.UNRESOLVED for f in unreachable.findings)
    assert unreachable.blocks_entry


def test_repeated_reconciliation_idempotent() -> None:
    _, orders, _, portfolio = _stack()
    recon = ReconciliationService(order_store=orders, portfolio=portfolio)
    a = recon.reconcile(venue_orders=[], venue_trades=[], venue_positions=[])
    b = recon.reconcile(venue_orders=[], venue_trades=[], venue_positions=[])
    assert a.counts() == b.counts()
    assert a.findings == b.findings


def test_user_stream_disconnect_removes_readiness_and_reconcile_restores() -> None:
    disp, orders, _, portfolio = _stack()
    transport = FakeTransport()
    oms = LiveOMS(
        transport=transport,
        dispatcher=disp,
        order_store=orders,
        portfolio=portfolio,
        mutations_enabled=False,
    )
    oms.mark_user_stream(True)
    assert ReadinessReason.USER_STREAM_UNREADY not in oms.readiness.reasons
    oms.mark_user_stream(False)
    assert ReadinessReason.USER_STREAM_UNREADY in oms.readiness.reasons
    assert ReadinessReason.RECONCILIATION_PENDING in oms.readiness.reasons
    report = oms.run_reconciliation()
    assert report is not None
    # Clean empty venue restores reconcile pending; mutations still disabled
    assert ReadinessReason.RECONCILIATION_PENDING not in oms.readiness.reasons
    assert ReadinessReason.MUTATIONS_DISABLED in oms.readiness.reasons


def test_missing_evidence_never_interpreted_as_flat() -> None:
    _, orders, _, portfolio = _stack()
    recon = ReconciliationService(order_store=orders, portfolio=portfolio)
    report = recon.reconcile(
        venue_orders=[],
        venue_trades=[],
        venue_positions=[],
        missing_evidence=True,
    )
    assert report.blocks_entry
    assert portfolio.is_flat()  # local empty
    # But reconcile must still block — missing evidence ≠ flat venue
    assert any("never assume flat" in f.detail for f in report.findings)


def test_position_mismatch_blocks_even_when_local_flat() -> None:
    from tyrex_pm.execution.polymarket.transport import VenuePositionSnapshot

    _, orders, _, portfolio = _stack()
    recon = ReconciliationService(order_store=orders, portfolio=portfolio)
    report = recon.reconcile(
        venue_orders=[],
        venue_trades=[],
        venue_positions=[
            VenuePositionSnapshot(instrument_token_id="tok", size=Decimal("5"))
        ],
    )
    assert report.blocks_entry
