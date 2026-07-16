"""Dry execution planner — converts approved EnterIntent to a plan (no submit).

Style: limit BUY at (tick-rounded) best ask.
Quantity: Q = floor(N / P) such that Q * P <= N (never exceeds target notional).
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from tyrex_pm.core.ids import EventId, TokenId
from tyrex_pm.core.intents import EnterIntent
from tyrex_pm.core.snapshots import BookSnapshot
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.market_data.executable import book_quote
from tyrex_pm.planning.plan import (
    ExecutionPlan,
    PlanFailReason,
    PlanningResult,
    PlanStatus,
    new_plan_id,
)
from tyrex_pm.risk.decision import RiskDecision


def round_buy_price_to_tick(price: Decimal, tick: Decimal | None) -> Decimal:
    if tick is None:
        return price
    if tick <= 0:
        raise ValueError("tick must be > 0")
    steps = (price / tick).to_integral_value(rounding=ROUND_UP)
    return steps * tick


def floor_quantity(notional: Decimal, price: Decimal) -> Decimal:
    if price <= 0:
        raise ValueError("price must be > 0")
    # Up to 6 decimal places; floor so Q * P never exceeds notional.
    return (notional / price).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)


def available_size_at_or_below(book: BookSnapshot, limit_price: Decimal) -> Decimal:
    total = Decimal("0")
    for level in book.asks:
        if level.price <= limit_price:
            total += level.quantity
        else:
            break
    return total


class ExecutionPlanner:
    def plan(
        self,
        intent: EnterIntent,
        *,
        risk: RiskDecision,
        market: BinaryMarket,
        book: BookSnapshot | None,
        now: datetime,
        causation_id: EventId | None = None,
    ) -> PlanningResult:
        if not risk.approved:
            return PlanningResult(
                status=PlanStatus.UNPLANNABLE,
                plan=None,
                fail_reason=PlanFailReason.RISK_NOT_APPROVED,
            )
        if book is None:
            return PlanningResult(
                status=PlanStatus.UNPLANNABLE,
                plan=None,
                fail_reason=PlanFailReason.MISSING_BOOK,
            )
        quote = book_quote(book)
        if quote.best_ask is None:
            return PlanningResult(
                status=PlanStatus.UNPLANNABLE,
                plan=None,
                fail_reason=PlanFailReason.ONE_SIDED_BOOK,
            )
        if quote.best_ask <= 0:
            return PlanningResult(
                status=PlanStatus.UNPLANNABLE,
                plan=None,
                fail_reason=PlanFailReason.PRICE_ZERO,
            )

        tick = market.tick_size
        try:
            limit_price = round_buy_price_to_tick(quote.best_ask, tick)
        except ValueError:
            return PlanningResult(
                status=PlanStatus.UNPLANNABLE,
                plan=None,
                fail_reason=PlanFailReason.INVALID_TICK,
            )

        max_price = intent.max_price
        if max_price is not None and limit_price > max_price:
            return PlanningResult(
                status=PlanStatus.UNPLANNABLE,
                plan=None,
                fail_reason=PlanFailReason.MAX_PRICE_EXCEEDED,
                evidence={"limit_price": str(limit_price), "max_price": str(max_price)},
            )

        try:
            quantity = floor_quantity(intent.target_notional, limit_price)
        except Exception:
            return PlanningResult(
                status=PlanStatus.UNPLANNABLE,
                plan=None,
                fail_reason=PlanFailReason.ROUNDING_FAILURE,
            )
        if quantity <= 0:
            return PlanningResult(
                status=PlanStatus.UNPLANNABLE,
                plan=None,
                fail_reason=PlanFailReason.ROUNDING_FAILURE,
                evidence={"quantity": str(quantity)},
            )

        expected_notional = quantity * limit_price
        if expected_notional > intent.target_notional:
            return PlanningResult(
                status=PlanStatus.UNPLANNABLE,
                plan=None,
                fail_reason=PlanFailReason.ROUNDING_FAILURE,
                evidence={"expected_notional": str(expected_notional)},
            )

        min_size = market.min_order_size
        if min_size is not None and quantity < min_size:
            return PlanningResult(
                status=PlanStatus.UNPLANNABLE,
                plan=None,
                fail_reason=PlanFailReason.BELOW_MIN_SIZE,
                evidence={"quantity": str(quantity), "min_order_size": str(min_size)},
            )

        available = available_size_at_or_below(book, limit_price)
        if available < quantity:
            return PlanningResult(
                status=PlanStatus.UNPLANNABLE,
                plan=None,
                fail_reason=PlanFailReason.INSUFFICIENT_DEPTH,
                evidence={"available": str(available), "quantity": str(quantity)},
            )

        token = (
            market.yes.token_id
            if intent.instrument_id == market.yes.instrument_id
            else market.no.token_id
        )
        plan = ExecutionPlan(
            plan_id=new_plan_id(),
            intent_id=intent.intent_id,
            instrument_id=intent.instrument_id,
            token_id=token if isinstance(token, TokenId) else TokenId(str(token)),
            market_id=intent.market_id,
            side=intent.side,
            quantity=quantity,
            limit_price=limit_price,
            expected_notional=expected_notional,
            book_ts_event=book.ts_event,
            tick_size=tick,
            min_order_size=min_size,
            planned_at=now,
            correlation_id=intent.correlation_id,
            causation_id=causation_id if causation_id is not None else intent.causation_id,
            evidence={
                "best_ask": str(quote.best_ask),
                "risk_decision_id": risk.decision_id.value,
                "style": "limit_buy_at_ask",
            },
        )
        return PlanningResult(status=PlanStatus.PLANNED, plan=plan)
