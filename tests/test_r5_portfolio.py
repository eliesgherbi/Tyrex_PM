"""R5 fill ledger + portfolio ownership."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tyrex_pm.core.commands import (
    ExecutionPolicy,
    SubmitOrderCommand,
    new_client_order_id,
    new_command_id,
)
from tyrex_pm.core.ids import StrategyId, new_correlation_id, new_order_id
from tyrex_pm.core.intents import IntentId, OrderSide
from tyrex_pm.execution.order_store import OrderStatus
from tyrex_pm.planning.plan import PlanId
from tyrex_pm.portfolio.portfolio import PortfolioError
from helpers_r5 import MARKET, T0, YES, make_book, submit_buy, wired_shadow


def test_buy_and_partial_sell() -> None:
    _, orders, ledger, portfolio, life, oms = wired_shadow()
    oms.on_book_updated(make_book(YES, bids=[("0.48", "100")], asks=[("0.52", "100")]))
    oid = new_order_id()
    life.note_entry_submitted(oid, YES, when=T0)
    oms.submit(submit_buy(qty="10"), order_id=oid)
    assert portfolio.net_quantity(YES) == Decimal("10")
    pos = portfolio.get(YES)
    assert pos is not None
    assert pos.average_entry_price == Decimal("0.52")
    assert len(ledger.all_fills()) == 1

    sell = SubmitOrderCommand(
        command_id=new_command_id(),
        plan_id=PlanId("plan-exit"),
        intent_id=IntentId("intent-exit"),
        strategy_id=StrategyId("reference_momentum"),
        instrument_id=YES,
        market_id=MARKET,
        side=OrderSide.SELL,
        quantity=Decimal("4"),
        limit_price=Decimal("0.48"),
        client_order_id=new_client_order_id(),
        created_at=T0,
        correlation_id=new_correlation_id(),
        causation_id=None,
        execution_policy=ExecutionPolicy.NORMAL,
    )
    eid = new_order_id()
    life.note_exit_submitted(eid, when=T0)
    oms.submit(sell, order_id=eid)
    assert portfolio.net_quantity(YES) == Decimal("6")
    assert life.state.value == "ACTIVE"
    assert orders.get(eid).status is OrderStatus.FILLED  # type: ignore[union-attr]


def test_full_exit_flat() -> None:
    _, _, _, portfolio, life, oms = wired_shadow()
    oms.on_book_updated(make_book(YES))
    oid = new_order_id()
    life.note_entry_submitted(oid, YES, when=T0)
    oms.submit(submit_buy(qty="5"), order_id=oid)
    sell = SubmitOrderCommand(
        command_id=new_command_id(),
        plan_id=PlanId("plan-exit"),
        intent_id=IntentId("intent-exit"),
        strategy_id=StrategyId("reference_momentum"),
        instrument_id=YES,
        market_id=MARKET,
        side=OrderSide.SELL,
        quantity=Decimal("5"),
        limit_price=Decimal("0.48"),
        client_order_id=new_client_order_id(),
        created_at=T0,
        correlation_id=new_correlation_id(),
        causation_id=None,
    )
    eid = new_order_id()
    life.note_exit_submitted(eid, when=T0)
    oms.submit(sell, order_id=eid)
    assert portfolio.net_quantity(YES) == 0
    assert portfolio.is_flat()
    assert life.state.value == "FLAT"


def test_oversell_rejected() -> None:
    _, _, _, portfolio, _, oms = wired_shadow()
    oms.on_book_updated(make_book(YES))
    oms.submit(submit_buy(qty="2"), order_id=new_order_id())
    sell = SubmitOrderCommand(
        command_id=new_command_id(),
        plan_id=PlanId("plan-exit"),
        intent_id=IntentId("intent-exit"),
        strategy_id=StrategyId("reference_momentum"),
        instrument_id=YES,
        market_id=MARKET,
        side=OrderSide.SELL,
        quantity=Decimal("9"),
        limit_price=Decimal("0.48"),
        client_order_id=new_client_order_id(),
        created_at=T0,
        correlation_id=new_correlation_id(),
        causation_id=None,
    )
    # ShadowOMS caps sell to position; portfolio oversell path tested directly.
    oms.submit(sell, order_id=new_order_id())
    assert portfolio.net_quantity(YES) == 0
    with pytest.raises(PortfolioError):
        portfolio._apply(
            instrument_id=YES,
            side=OrderSide.SELL,
            qty=Decimal("1"),
            price=Decimal("0.4"),
            fee=Decimal("0"),
            when=T0,
        )
