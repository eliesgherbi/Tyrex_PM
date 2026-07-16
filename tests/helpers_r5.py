"""Shared builders for R5 unit tests."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.commands import (
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
from tyrex_pm.core.snapshots import BookSnapshot
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.execution.fill_ledger import FillLedger
from tyrex_pm.execution.order_store import OrderStore
from tyrex_pm.execution.shadow_oms import ShadowFillConfig, ShadowOMS
from tyrex_pm.lifecycle.trade_lifecycle import TradeLifecycle
from tyrex_pm.planning.plan import PlanId
from tyrex_pm.portfolio.portfolio import Portfolio

T0 = datetime(2026, 7, 16, 12, 1, 0, tzinfo=timezone.utc)
YES = InstrumentId("tok-yes-1")
NO = InstrumentId("tok-no-1")
MARKET = MarketId("cond-fixture-1")


def make_book(
    instrument: InstrumentId,
    *,
    bids: list[tuple[str, str]] | None = None,
    asks: list[tuple[str, str]] | None = None,
    ts: datetime | None = None,
) -> BookSnapshot:
    when = ts or T0
    return BookSnapshot.from_levels(
        instrument_id=instrument,
        ts_event=when,
        bids=bids or [("0.48", "100")],
        asks=asks or [("0.52", "100")],
    )


def wired_shadow(
    *,
    cancel_residual: bool = False,
) -> tuple[EventDispatcher, OrderStore, FillLedger, Portfolio, TradeLifecycle, ShadowOMS]:
    disp = EventDispatcher()
    orders = OrderStore()
    ledger = FillLedger()
    portfolio = Portfolio(fill_ledger=ledger, market_id=MARKET)
    life = TradeLifecycle(order_store=orders, portfolio=portfolio)
    oms = ShadowOMS(
        dispatcher=disp,
        order_store=orders,
        portfolio=portfolio,
        config=ShadowFillConfig(cancel_unfilled_residual=cancel_residual),
    )
    orders.attach(disp)
    ledger.attach(disp)
    portfolio.attach(disp)
    life.attach(disp)
    return disp, orders, ledger, portfolio, life, oms


def submit_buy(
    *,
    qty: str = "10",
    limit: str = "0.52",
    instrument: InstrumentId = YES,
    when: datetime | None = None,
    corr: CorrelationId | None = None,
) -> SubmitOrderCommand:
    return SubmitOrderCommand(
        command_id=new_command_id(),
        plan_id=PlanId("plan-1"),
        intent_id=IntentId("intent-1"),
        strategy_id=StrategyId("reference_momentum"),
        instrument_id=instrument,
        market_id=MARKET,
        side=OrderSide.BUY,
        quantity=Decimal(qty),
        limit_price=Decimal(limit),
        client_order_id=new_client_order_id(),
        created_at=when or T0,
        correlation_id=corr or new_correlation_id(),
        causation_id=None,
        execution_policy=ExecutionPolicy.NORMAL,
    )
