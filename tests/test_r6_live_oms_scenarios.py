"""R6A LiveOMS scripted transport scenarios (mutations enabled against FakeTransport)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.commands import (
    CancelOrderCommand,
    ExecutionPolicy,
    SubmitOrderCommand,
    new_client_order_id,
    new_command_id,
)
from tyrex_pm.core.ids import (
    CorrelationId,
    InstrumentId,
    MarketId,
    StrategyId,
    new_correlation_id,
)
from tyrex_pm.core.intents import IntentId, OrderSide
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.execution.fill_ledger import FillLedger
from tyrex_pm.execution.order_store import OrderStatus, OrderStore
from tyrex_pm.execution.polymarket.fake_transport import FakeTransport
from tyrex_pm.execution.polymarket.live_oms import LiveOMS, SubmissionState
from tyrex_pm.execution.polymarket.readiness import ReadinessReason
from tyrex_pm.planning.plan import PlanId
from tyrex_pm.portfolio.portfolio import Portfolio

T0 = datetime(2026, 7, 16, 20, 0, 0, tzinfo=timezone.utc)
TOKEN = InstrumentId("tok-yes-1")
MARKET = MarketId("m1")


def _wired(*, mutations: bool = True):
    disp = EventDispatcher()
    orders = OrderStore()
    ledger = FillLedger()
    portfolio = Portfolio(fill_ledger=ledger, market_id=MARKET)
    orders.attach(disp)
    ledger.attach(disp)
    portfolio.attach(disp)
    transport = FakeTransport()
    facts: list[tuple[str, dict]] = []
    oms = LiveOMS(
        transport=transport,
        dispatcher=disp,
        order_store=orders,
        portfolio=portfolio,
        mutations_enabled=mutations,
        emit_fact=lambda t, p: facts.append((t, p)),
    )
    return disp, orders, portfolio, transport, oms, facts


def _cmd(**kw) -> SubmitOrderCommand:
    base = dict(
        command_id=new_command_id(),
        plan_id=PlanId("p1"),
        intent_id=IntentId("i1"),
        strategy_id=StrategyId("reference_momentum"),
        instrument_id=TOKEN,
        market_id=MARKET,
        side=OrderSide.BUY,
        quantity=Decimal("5"),
        limit_price=Decimal("0.52"),
        client_order_id=new_client_order_id(),
        created_at=T0,
        correlation_id=new_correlation_id(),
        causation_id=None,
        execution_policy=ExecutionPolicy.NORMAL,
    )
    base.update(kw)
    return SubmitOrderCommand(**base)


def test_mutations_disabled_rejects_without_transport_call() -> None:
    _, orders, _, transport, oms, facts = _wired(mutations=False)
    oid = oms.submit(_cmd())
    assert transport._submit_count == 0
    assert orders.get(oid).status is OrderStatus.REJECTED  # type: ignore[union-attr]
    assert any(t == "submission_denied" for t, _ in facts)


def test_submit_accepted() -> None:
    _, orders, _, transport, oms, _ = _wired()
    oid = oms.submit(_cmd())
    rec = orders.get(oid)
    assert rec is not None
    assert rec.status is OrderStatus.ACCEPTED
    assert rec.venue_order_id is not None
    assert oms._tracking[oid.value].submission is SubmissionState.VENUE_ACCEPTED


def test_submit_rejected() -> None:
    _, orders, _, transport, oms, _ = _wired()
    transport.submit_behavior = "reject"
    oid = oms.submit(_cmd())
    assert orders.get(oid).status is OrderStatus.REJECTED  # type: ignore[union-attr]


def test_submit_timeout_uncertain_then_reconcile() -> None:
    _, orders, _, transport, oms, facts = _wired()
    transport.submit_behavior = "timeout_later"
    oid = oms.submit(_cmd())
    assert oms._tracking[oid.value].submission is SubmissionState.UNKNOWN_SUBMISSION
    assert ReadinessReason.UNKNOWN_SUBMISSION in oms.readiness.reasons
    assert orders.get(oid).status is OrderStatus.SUBMITTED  # type: ignore[union-attr]
    transport.resolve_timeout_as_accepted()
    oms.run_reconciliation()
    assert oms._tracking[oid.value].submission is SubmissionState.VENUE_ACCEPTED
    assert orders.get(oid).status is OrderStatus.ACCEPTED  # type: ignore[union-attr]
    assert any(t == "submission_resolved" for t, _ in facts)


def test_submit_timeout_unresolved_blocks() -> None:
    _, _, _, transport, oms, facts = _wired()
    transport.submit_behavior = "timeout"
    oms.submit(_cmd())
    report = oms.run_reconciliation()
    # No unique open order to adopt
    assert oms.has_uncertain_submission()
    assert any(t == "manual_intervention_required" for t, _ in facts)
    assert report is not None


def test_cancel_accepted() -> None:
    _, orders, _, transport, oms, _ = _wired()
    oid = oms.submit(_cmd())
    oms.cancel(
        CancelOrderCommand(
            command_id=new_command_id(),
            order_id=oid,
            reason_code="USER",
            created_at=T0,
            correlation_id=CorrelationId("c"),
            causation_id=None,
        )
    )
    assert orders.get(oid).status is OrderStatus.CANCELED  # type: ignore[union-attr]


def test_cancel_after_mutations_disabled() -> None:
    _, _, _, transport, oms, facts = _wired(mutations=False)
    # Force-create accepted path isn't available; cancel denied still records fact
    from tyrex_pm.core.ids import new_order_id

    oid = new_order_id()
    orders = oms.order_store
    orders.create_from_command(_cmd(), order_id=oid)
    oms.cancel(
        CancelOrderCommand(
            command_id=new_command_id(),
            order_id=oid,
            reason_code="USER",
            created_at=T0,
            correlation_id=CorrelationId("c"),
            causation_id=None,
        )
    )
    assert transport._cancel_count == 0
    assert any(t == "cancel_denied" for t, _ in facts)
