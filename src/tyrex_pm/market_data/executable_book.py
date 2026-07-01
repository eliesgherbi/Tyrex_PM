"""ExecutableBookView and PlannerEvidence (Phase 2 M4).

Every FAK plan / retry must capture a fresh store snapshot and emit new evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import uuid4

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.execution.models import ExecutionPlanResult
from tyrex_pm.market_data.models import MarketStateSnapshot
from tyrex_pm.market_data.quality import (
    DataQualityGate,
    DataQualityReport,
    DecisionContext,
    QualityVerdict,
)
from tyrex_pm.state.market_store import MarketStateStore


@dataclass(frozen=True)
class ExecutableBookView:
    side: Side
    size: Decimal
    touch_price: Decimal | None
    worst_price_to_fill: Decimal | None
    sweep_vwap: Decimal | None
    available_depth: Decimal
    levels_consumed: int
    snapshot_id: str

    @staticmethod
    def from_snapshot(snap: MarketStateSnapshot, *, side: Side, size: Decimal) -> ExecutableBookView:
        levels = snap.asks if side == Side.BUY else snap.bids
        touch = snap.best_ask if side == Side.BUY else snap.best_bid
        remaining = size
        cost = Decimal("0")
        filled = Decimal("0")
        consumed = 0
        worst: Decimal | None = None
        for lv in levels:
            if remaining <= 0:
                break
            take = lv.size if lv.size < remaining else remaining
            cost += take * lv.price
            filled += take
            remaining -= take
            consumed += 1
            worst = lv.price
        sweep = (cost / filled) if filled > 0 else None
        return ExecutableBookView(
            side=side,
            size=size,
            touch_price=touch,
            worst_price_to_fill=worst,
            sweep_vwap=sweep,
            available_depth=filled,
            levels_consumed=consumed,
            snapshot_id=snap.snapshot_id,
        )


@dataclass(frozen=True)
class PlannerEvidence:
    decision_id: str
    snapshot_id: str
    book_age_ms: int
    source: str
    touch_price: Decimal | None
    worst_price_to_fill: Decimal | None
    sweep_vwap: Decimal | None
    expected_slippage: Decimal | None
    available_depth: Decimal
    quality_verdict: str
    emergency_reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "snapshot_id": self.snapshot_id,
            "book_age_ms": self.book_age_ms,
            "source": self.source,
            "touch_price": str(self.touch_price) if self.touch_price is not None else None,
            "worst_price_to_fill": str(self.worst_price_to_fill)
            if self.worst_price_to_fill is not None
            else None,
            "sweep_vwap": str(self.sweep_vwap) if self.sweep_vwap is not None else None,
            "expected_slippage": str(self.expected_slippage)
            if self.expected_slippage is not None
            else None,
            "available_depth": str(self.available_depth),
            "quality_verdict": self.quality_verdict,
            "emergency_reason": self.emergency_reason,
        }


@dataclass(frozen=True)
class FakPlanAttempt:
    """Bundle produced for each fresh-snapshot FAK plan or retry."""

    decision_id: str
    snapshot: MarketStateSnapshot
    quality_report: DataQualityReport
    executable_view: ExecutableBookView
    planner_evidence: PlannerEvidence
    plan_result: ExecutionPlanResult


def build_planner_evidence(
    *,
    decision_id: str,
    snap: MarketStateSnapshot,
    view: ExecutableBookView,
    quality_report: DataQualityReport,
) -> PlannerEvidence:
    slippage = _expected_slippage(view)
    return PlannerEvidence(
        decision_id=decision_id,
        snapshot_id=snap.snapshot_id,
        book_age_ms=snap.book_age_ms,
        source=snap.source,
        touch_price=view.touch_price,
        worst_price_to_fill=view.worst_price_to_fill,
        sweep_vwap=view.sweep_vwap,
        expected_slippage=slippage,
        available_depth=view.available_depth,
        quality_verdict=quality_report.verdict.value,
        emergency_reason=quality_report.emergency_reason,
    )


def plan_fak_with_fresh_evidence(
    planner,
    approved,
    *,
    market_state: MarketStateStore,
    token_id: TokenId,
    side: Side,
    size: Decimal,
    decision_context: DecisionContext,
    quality_gate: DataQualityGate,
    decision_id: str | None = None,
    now=None,
) -> FakPlanAttempt | None:
    """Capture fresh snapshot, evaluate quality, plan FAK from executable depth."""
    snap = market_state.capture(token_id, now=now or utc_now())
    if snap is None:
        return None
    did = decision_id or str(uuid4())
    report = quality_gate.evaluate_snapshot(snap, context=decision_context, size=size)
    view = ExecutableBookView.from_snapshot(snap, side=side, size=size)
    evidence = build_planner_evidence(
        decision_id=did, snap=snap, view=view, quality_report=report
    )
    if not quality_gate.allows_decision(report, decision_context):
        return FakPlanAttempt(
            decision_id=did,
            snapshot=snap,
            quality_report=report,
            executable_view=view,
            planner_evidence=evidence,
            plan_result=ExecutionPlanResult(
                approved=False,
                reason="quality_reject",
                plan=None,
                evidence={**evidence.to_payload(), "quality_reasons": list(report.reasons)},
            ),
        )
    result = planner.plan(
        approved,
        market_state=market_state,
        now=now,
        captured_snapshot=snap,
        executable_view=view,
        quality_report=report,
        planner_evidence=evidence,
        decision_context=decision_context,
    )
    return FakPlanAttempt(
        decision_id=did,
        snapshot=snap,
        quality_report=report,
        executable_view=view,
        planner_evidence=evidence,
        plan_result=result,
    )


def plan_fak_retry_for_remaining(
    planner,
    approved,
    *,
    market_state: MarketStateStore,
    token_id: TokenId,
    side: Side,
    original_qty: Decimal,
    filled_qty: Decimal,
    decision_context: DecisionContext,
    quality_gate: DataQualityGate,
    now=None,
) -> FakPlanAttempt | None:
    """Partial-fill / FAK-reject retry: fresh snapshot at remaining size."""
    remaining = original_qty - filled_qty
    if remaining <= 0:
        return None
    from tyrex_pm.execution.models import restyle_intent

    retry_intent = restyle_intent(approved.intent, order_style=approved.intent.order_style, limit_price=approved.intent.limit_price, size=remaining)
    retry_approved = type(approved)(
        intent=retry_intent,
        client_order_id=approved.client_order_id,
        run_id=approved.run_id,
    )
    return plan_fak_with_fresh_evidence(
        planner,
        retry_approved,
        market_state=market_state,
        token_id=token_id,
        side=side,
        size=remaining,
        decision_context=decision_context,
        quality_gate=quality_gate,
        decision_id=str(uuid4()),
        now=now,
    )


def _expected_slippage(view: ExecutableBookView) -> Decimal | None:
    if view.touch_price is None or view.sweep_vwap is None:
        return None
    return abs(view.sweep_vwap - view.touch_price)
