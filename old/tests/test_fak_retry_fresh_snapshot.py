"""FAK retry must capture fresh snapshots — never reuse evidence."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import ClientOrderId, RunId, TokenId
from tyrex_pm.core.models import ApprovedIntent, ExitIntent, URGENCY_URGENT
from tyrex_pm.core.time import utc_now
from tyrex_pm.execution.planner import ExecutionPlanner
from tyrex_pm.market_data.executable_book import plan_fak_retry_for_remaining, plan_fak_with_fresh_evidence
from tyrex_pm.market_data.models import BookLevel, BookSource
from tyrex_pm.market_data.quality import CRYPTO_5M_PROFILE, DataQualityGate, DataQualityGateConfig, DecisionContext
from tyrex_pm.runtime.config import ExecutionPlannerConfig
from tyrex_pm.state.market_store import MarketStateStore

TOKEN = TokenId("tok-retry")


def _approved(size: Decimal) -> ApprovedIntent:
    intent = ExitIntent(
        token_id=TOKEN,
        side=Side.SELL,
        size=size,
        limit_price=Decimal("0.4"),
        order_style=OrderStyle.FAK,
        urgency=URGENCY_URGENT,
    )
    return ApprovedIntent(intent=intent, client_order_id=ClientOrderId("c1"), run_id=RunId("r1"))


def _planner_and_gate():
    gate = DataQualityGate(DataQualityGateConfig(profiles={"crypto_5m": CRYPTO_5M_PROFILE}))
    planner = ExecutionPlanner(
        ExecutionPlannerConfig(enabled=True, use_executable_depth=True, max_book_age_s=60),
        quality_gate=gate,
    )
    return planner, gate


def test_two_retries_use_different_snapshot_ids() -> None:
    store = MarketStateStore()
    store.apply_book(
        TOKEN,
        [BookLevel(Decimal("0.49"), Decimal("100"))],
        [BookLevel(Decimal("0.51"), Decimal("100"))],
        source=BookSource.WEBSOCKET,
        received_ts=utc_now(),
    )
    planner, gate = _planner_and_gate()
    ap = _approved(Decimal("50"))
    first = plan_fak_with_fresh_evidence(
        planner,
        ap,
        market_state=store,
        token_id=TOKEN,
        side=Side.SELL,
        size=Decimal("50"),
        decision_context=DecisionContext.URGENT_EXIT,
        quality_gate=gate,
    )
    assert first is not None
    store.apply_book(
        TOKEN,
        [BookLevel(Decimal("0.48"), Decimal("100"))],
        [BookLevel(Decimal("0.51"), Decimal("100"))],
        source=BookSource.WEBSOCKET,
        received_ts=utc_now(),
    )
    second = plan_fak_with_fresh_evidence(
        planner,
        ap,
        market_state=store,
        token_id=TOKEN,
        side=Side.SELL,
        size=Decimal("50"),
        decision_context=DecisionContext.URGENT_EXIT,
        quality_gate=gate,
    )
    assert second is not None
    assert first.snapshot.snapshot_id != second.snapshot.snapshot_id
    assert first.decision_id != second.decision_id
    assert first.planner_evidence is not second.planner_evidence


def test_partial_fill_retry_sizes_remaining_qty() -> None:
    store = MarketStateStore()
    store.apply_book(
        TOKEN,
        [BookLevel(Decimal("0.49"), Decimal("100"))],
        [],
        source=BookSource.WEBSOCKET,
        received_ts=utc_now(),
    )
    planner, gate = _planner_and_gate()
    ap = _approved(Decimal("50"))
    retry = plan_fak_retry_for_remaining(
        planner,
        ap,
        market_state=store,
        token_id=TOKEN,
        side=Side.SELL,
        original_qty=Decimal("50"),
        filled_qty=Decimal("20"),
        decision_context=DecisionContext.STOP,
        quality_gate=gate,
    )
    assert retry is not None
    assert retry.executable_view.size == Decimal("30")
    assert retry.plan_result.plan is not None
    assert retry.plan_result.plan.size == Decimal("30")


def test_fak_reject_retry_does_not_reuse_worst_price() -> None:
    store = MarketStateStore()
    store.apply_book(
        TOKEN,
        [BookLevel(Decimal("0.49"), Decimal("10")), BookLevel(Decimal("0.40"), Decimal("100"))],
        [],
        source=BookSource.WEBSOCKET,
        received_ts=utc_now(),
    )
    planner, gate = _planner_and_gate()
    ap = _approved(Decimal("50"))
    first = plan_fak_with_fresh_evidence(
        planner,
        ap,
        market_state=store,
        token_id=TOKEN,
        side=Side.SELL,
        size=Decimal("50"),
        decision_context=DecisionContext.URGENT_EXIT,
        quality_gate=gate,
    )
    assert first is not None
    first_worst = first.executable_view.worst_price_to_fill
    store.apply_book(
        TOKEN,
        [BookLevel(Decimal("0.45"), Decimal("100"))],
        [],
        source=BookSource.WEBSOCKET,
        received_ts=utc_now(),
    )
    second = plan_fak_retry_for_remaining(
        planner,
        ap,
        market_state=store,
        token_id=TOKEN,
        side=Side.SELL,
        original_qty=Decimal("50"),
        filled_qty=Decimal("0"),
        decision_context=DecisionContext.URGENT_EXIT,
        quality_gate=gate,
    )
    assert second is not None
    assert second.executable_view.worst_price_to_fill != first_worst
