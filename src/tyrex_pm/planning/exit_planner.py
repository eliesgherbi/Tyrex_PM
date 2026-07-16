"""Dry exit plan: limit SELL at tick-rounded best bid (target-flat)."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from tyrex_pm.core.ids import EventId, TokenId
from tyrex_pm.core.intents import ExitIntent, FlattenIntent, OrderSide
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


def round_sell_price_to_tick(price: Decimal, tick: Decimal | None) -> Decimal:
    if tick is None:
        return price
    if tick <= 0:
        raise ValueError("tick must be > 0")
    steps = (price / tick).to_integral_value(rounding=ROUND_DOWN)
    return steps * tick


class ExitPlanner:
    def plan(
        self,
        intent: ExitIntent | FlattenIntent,
        *,
        risk: RiskDecision,
        market: BinaryMarket,
        book: BookSnapshot | None,
        position_qty: Decimal,
        now: datetime,
        causation_id: EventId | None = None,
    ) -> PlanningResult:
        if not risk.approved:
            return PlanningResult(
                status=PlanStatus.UNPLANNABLE,
                plan=None,
                fail_reason=PlanFailReason.RISK_NOT_APPROVED,
            )
        if position_qty <= 0:
            return PlanningResult(
                status=PlanStatus.UNPLANNABLE,
                plan=None,
                fail_reason=PlanFailReason.ROUNDING_FAILURE,
                evidence={"position_qty": str(position_qty)},
            )
        if book is None:
            return PlanningResult(
                status=PlanStatus.UNPLANNABLE,
                plan=None,
                fail_reason=PlanFailReason.MISSING_BOOK,
            )
        quote = book_quote(book)
        if quote.best_bid is None or quote.best_bid <= 0:
            return PlanningResult(
                status=PlanStatus.UNPLANNABLE,
                plan=None,
                fail_reason=PlanFailReason.ONE_SIDED_BOOK,
            )
        try:
            limit_price = round_sell_price_to_tick(quote.best_bid, market.tick_size)
        except ValueError:
            return PlanningResult(
                status=PlanStatus.UNPLANNABLE,
                plan=None,
                fail_reason=PlanFailReason.INVALID_TICK,
            )
        if isinstance(intent, ExitIntent) and intent.min_price is not None:
            if limit_price < intent.min_price:
                return PlanningResult(
                    status=PlanStatus.UNPLANNABLE,
                    plan=None,
                    fail_reason=PlanFailReason.MAX_PRICE_EXCEEDED,
                    evidence={"limit_price": str(limit_price), "min_price": str(intent.min_price)},
                )

        quantity = position_qty.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        if quantity <= 0:
            return PlanningResult(
                status=PlanStatus.UNPLANNABLE,
                plan=None,
                fail_reason=PlanFailReason.ROUNDING_FAILURE,
            )
        min_size = market.min_order_size
        if min_size is not None and quantity < min_size:
            # Still allow emergency flatten of residual dust via urgent policy.
            if not isinstance(intent, FlattenIntent):
                return PlanningResult(
                    status=PlanStatus.UNPLANNABLE,
                    plan=None,
                    fail_reason=PlanFailReason.BELOW_MIN_SIZE,
                    evidence={"quantity": str(quantity), "min_order_size": str(min_size)},
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
            side=OrderSide.SELL,
            quantity=quantity,
            limit_price=limit_price,
            expected_notional=quantity * limit_price,
            book_ts_event=book.ts_event,
            tick_size=market.tick_size,
            min_order_size=min_size,
            planned_at=now,
            correlation_id=intent.correlation_id,
            causation_id=causation_id if causation_id is not None else intent.causation_id,
            evidence={
                "best_bid": str(quote.best_bid),
                "risk_decision_id": risk.decision_id.value,
                "style": "limit_sell_at_bid",
                "intent_kind": intent.kind.value,
            },
        )
        return PlanningResult(status=PlanStatus.PLANNED, plan=plan)
