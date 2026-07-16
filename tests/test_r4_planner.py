"""R4 dry execution planner tests."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.ids import MarketId, TokenId, new_correlation_id, new_event_id
from tyrex_pm.core.intents import EnterIntent, new_intent_id
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.core.snapshots import BookLevel, BookSnapshot
from tyrex_pm.domain.polymarket.market import BinaryMarket, MarketStatus, make_binary_instruments
from tyrex_pm.planning.plan import PlanFailReason, PlanStatus
from tyrex_pm.planning.planner import ExecutionPlanner, floor_quantity, round_buy_price_to_tick
from tyrex_pm.risk.decision import RiskDecision, new_decision_id
from tyrex_pm.risk.reasons import RiskReason
from tyrex_pm.strategies.framework_validation.reference_momentum import ReferenceMomentumStrategy

TS = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def _market(min_size: Decimal | None = Decimal("1")) -> BinaryMarket:
    mid = MarketId("m1")
    yes, no = make_binary_instruments(market_id=mid, yes_token=TokenId("y"), no_token=TokenId("n"))
    return BinaryMarket(
        market_id=mid,
        condition_id="m1",
        question="q",
        yes=yes,
        no=no,
        status=MarketStatus.ACTIVE,
        tick_size=Decimal("0.01"),
        min_order_size=min_size,
    )


def _intent(market: BinaryMarket, notional: str = "2") -> EnterIntent:
    return EnterIntent(
        intent_id=new_intent_id(),
        strategy_id=ReferenceMomentumStrategy.STRATEGY_ID,
        instrument_id=market.yes.instrument_id,
        market_id=market.market_id,
        created_at=TS,
        correlation_id=new_correlation_id(),
        causation_id=new_event_id(),
        reason_code="MOMENTUM_UP",
        target_notional=Decimal(notional),
        outcome=OutcomeSide.YES,
        decision_epoch=1,
        max_price=Decimal("0.99"),
    )


def _approved(intent: EnterIntent) -> RiskDecision:
    return RiskDecision(
        decision_id=new_decision_id(),
        intent_id=intent.intent_id,
        approved=True,
        mode=RuntimeMode.SHADOW,
        reason_codes=(RiskReason.APPROVED,),
        policy_results=(),
        evaluated_at=TS,
        correlation_id=intent.correlation_id,
        causation_id=intent.causation_id,
    )


def test_tick_and_quantity() -> None:
    assert round_buy_price_to_tick(Decimal("0.521"), Decimal("0.01")) == Decimal("0.53")
    q = floor_quantity(Decimal("2"), Decimal("0.52"))
    assert q * Decimal("0.52") <= Decimal("2")


def test_planned_never_exceeds_notional() -> None:
    market = _market()
    intent = _intent(market, "2")
    book = BookSnapshot(
        instrument_id=market.yes.instrument_id,
        ts_event=TS,
        bids=(BookLevel(price=Decimal("0.48"), quantity=Decimal("100")),),
        asks=(BookLevel(price=Decimal("0.52"), quantity=Decimal("100")),),
    )
    result = ExecutionPlanner().plan(
        intent, risk=_approved(intent), market=market, book=book, now=TS
    )
    assert result.status is PlanStatus.PLANNED
    assert result.plan is not None
    assert result.plan.expected_notional <= intent.target_notional
    assert result.plan.limit_price == Decimal("0.52")


def test_below_min_size() -> None:
    market = _market(min_size=Decimal("100"))
    intent = _intent(market, "2")
    book = BookSnapshot(
        instrument_id=market.yes.instrument_id,
        ts_event=TS,
        bids=(BookLevel(price=Decimal("0.48"), quantity=Decimal("100")),),
        asks=(BookLevel(price=Decimal("0.52"), quantity=Decimal("100")),),
    )
    result = ExecutionPlanner().plan(
        intent, risk=_approved(intent), market=market, book=book, now=TS
    )
    assert result.status is PlanStatus.UNPLANNABLE
    assert result.fail_reason is PlanFailReason.BELOW_MIN_SIZE


def test_empty_book_unplannable() -> None:
    market = _market()
    intent = _intent(market)
    result = ExecutionPlanner().plan(
        intent, risk=_approved(intent), market=market, book=None, now=TS
    )
    assert result.fail_reason is PlanFailReason.MISSING_BOOK


def test_price_zero() -> None:
    market = _market()
    intent = _intent(market)
    book = BookSnapshot(
        instrument_id=market.yes.instrument_id,
        ts_event=TS,
        bids=(),
        asks=(BookLevel(price=Decimal("0"), quantity=Decimal("10")),),
    )
    result = ExecutionPlanner().plan(
        intent, risk=_approved(intent), market=market, book=book, now=TS
    )
    assert result.fail_reason is PlanFailReason.PRICE_ZERO


def test_insufficient_depth() -> None:
    market = _market(min_size=Decimal("1"))
    intent = _intent(market, "2")
    book = BookSnapshot(
        instrument_id=market.yes.instrument_id,
        ts_event=TS,
        bids=(BookLevel(price=Decimal("0.48"), quantity=Decimal("100")),),
        asks=(BookLevel(price=Decimal("0.52"), quantity=Decimal("1")),),
    )
    result = ExecutionPlanner().plan(
        intent, risk=_approved(intent), market=market, book=book, now=TS
    )
    assert result.fail_reason is PlanFailReason.INSUFFICIENT_DEPTH


def test_max_price() -> None:
    market = _market()
    intent = EnterIntent(
        intent_id=new_intent_id(),
        strategy_id=ReferenceMomentumStrategy.STRATEGY_ID,
        instrument_id=market.yes.instrument_id,
        market_id=market.market_id,
        created_at=TS,
        correlation_id=new_correlation_id(),
        causation_id=new_event_id(),
        reason_code="X",
        target_notional=Decimal("2"),
        outcome=OutcomeSide.YES,
        decision_epoch=1,
        max_price=Decimal("0.40"),
    )
    book = BookSnapshot(
        instrument_id=market.yes.instrument_id,
        ts_event=TS,
        bids=(BookLevel(price=Decimal("0.30"), quantity=Decimal("100")),),
        asks=(BookLevel(price=Decimal("0.52"), quantity=Decimal("100")),),
    )
    result = ExecutionPlanner().plan(
        intent, risk=_approved(intent), market=market, book=book, now=TS
    )
    assert result.fail_reason is PlanFailReason.MAX_PRICE_EXCEEDED
