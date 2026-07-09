"""Depth-aware executable survival exit adapter (Phase 1 M2)."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.execution.planner import build_quality_gate_from_config
from tyrex_pm.market_data.executable_book import ExecutableBookView, build_planner_evidence
from tyrex_pm.market_data.quality import DataQualityGate, DecisionContext
from tyrex_pm.runtime.config import SurvivalExitPlanningConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.survival.models import ExecutableExitEvidence, SurvivalExitEvaluation
from tyrex_pm.survival.quality_reject_detail import build_quality_reject_detail


class SurvivalExitPlanner:
    def __init__(
        self,
        exit_cfg: SurvivalExitPlanningConfig,
        *,
        quality_gate: DataQualityGate | None = None,
        default_max_book_age_s: float | None = None,
    ) -> None:
        self._cfg = exit_cfg
        self._quality_gate = quality_gate
        self._default_max_book_age_s = default_max_book_age_s

    def evaluate_exit(
        self,
        *,
        coord: RuntimeCoordinator,
        token_id: TokenId,
        qty: Decimal,
        decision_context: DecisionContext = DecisionContext.URGENT_EXIT,
        max_book_age_s: float | None = None,
    ) -> SurvivalExitEvaluation:
        empty = self._empty_evidence(snapshot_id=str(uuid4()))
        store = coord.market_state
        if store is None:
            return SurvivalExitEvaluation(
                verdict="blocked",
                recommended_qty=Decimal("0"),
                evidence=empty,
                reason="no_market_store",
            )

        snap = store.capture(token_id, now=utc_now())
        if snap is None:
            return SurvivalExitEvaluation(
                verdict="blocked",
                recommended_qty=Decimal("0"),
                evidence=empty,
                reason="no_snapshot",
            )

        view = ExecutableBookView.from_snapshot(snap, side=Side.SELL, size=qty)
        gate = self._quality_gate or DataQualityGate()
        report = gate.evaluate_snapshot(snap, context=decision_context, size=qty)
        planner_ref = None
        if self._cfg.require_executable_evidence:
            evidence = build_planner_evidence(
                decision_id=str(uuid4()),
                snap=snap,
                view=view,
                quality_report=report,
            )
            planner_ref = evidence.to_payload()

        depth_fraction = (
            (view.available_depth / qty) if qty > 0 else Decimal("0")
        )
        slippage = None
        if view.touch_price is not None and view.sweep_vwap is not None:
            slippage = abs(view.sweep_vwap - view.touch_price)

        exit_evidence = ExecutableExitEvidence(
            touch_bid=view.touch_price,
            executable_bid=view.worst_price_to_fill or view.sweep_vwap,
            sweep_vwap=view.sweep_vwap,
            worst_price_to_fill=view.worst_price_to_fill,
            available_depth=view.available_depth,
            available_depth_fraction=depth_fraction,
            expected_slippage=slippage,
            book_age_ms=snap.book_age_ms,
            snapshot_id=snap.snapshot_id,
            quality_verdict=report.verdict.value,
            spread=snap.spread,
            planner_evidence_ref=planner_ref,
        )

        age_limit_s = max_book_age_s
        if age_limit_s is None:
            age_limit_s = self._cfg.max_book_age_s
        if age_limit_s is None:
            age_limit_s = self._default_max_book_age_s
        if age_limit_s is not None and snap.book_age_ms > int(age_limit_s * 1000):
            return SurvivalExitEvaluation(
                verdict="defer",
                recommended_qty=Decimal("0"),
                evidence=exit_evidence,
                reason="stale_book",
            )

        if self._cfg.require_executable_evidence and not gate.allows_decision(
            report, decision_context
        ):
            reject_detail = build_quality_reject_detail(report)
            exit_evidence = ExecutableExitEvidence(
                touch_bid=exit_evidence.touch_bid,
                executable_bid=exit_evidence.executable_bid,
                sweep_vwap=exit_evidence.sweep_vwap,
                worst_price_to_fill=exit_evidence.worst_price_to_fill,
                available_depth=exit_evidence.available_depth,
                available_depth_fraction=exit_evidence.available_depth_fraction,
                expected_slippage=exit_evidence.expected_slippage,
                book_age_ms=exit_evidence.book_age_ms,
                snapshot_id=exit_evidence.snapshot_id,
                quality_verdict=exit_evidence.quality_verdict,
                spread=exit_evidence.spread,
                planner_evidence_ref=exit_evidence.planner_evidence_ref,
                quality_reject_detail=reject_detail,
            )
            return SurvivalExitEvaluation(
                verdict="defer",
                recommended_qty=Decimal("0"),
                evidence=exit_evidence,
                reason="quality_reject",
            )

        if depth_fraction >= self._cfg.min_depth_fraction:
            return SurvivalExitEvaluation(
                verdict="proceed_full",
                recommended_qty=qty,
                evidence=exit_evidence,
                reason=None,
            )

        if self._cfg.allow_partial_survival_exit and view.available_depth > 0:
            partial = view.available_depth if view.available_depth < qty else qty
            return SurvivalExitEvaluation(
                verdict="proceed_partial",
                recommended_qty=partial,
                evidence=exit_evidence,
                reason="partial_depth",
            )

        return SurvivalExitEvaluation(
            verdict="defer",
            recommended_qty=Decimal("0"),
            evidence=exit_evidence,
            reason="insufficient_depth",
        )

    def executable_bid_for_progress(
        self,
        evaluation: SurvivalExitEvaluation,
    ) -> Decimal | None:
        """Authoritative bid for progress — never touch alone when depth is thin."""
        if evaluation.verdict in ("defer", "blocked"):
            return None
        ev = evaluation.evidence
        if ev.available_depth_fraction >= Decimal("1"):
            return ev.executable_bid or ev.sweep_vwap or ev.touch_bid
        if ev.sweep_vwap is not None:
            return ev.sweep_vwap
        if ev.worst_price_to_fill is not None:
            return ev.worst_price_to_fill
        return None

    @staticmethod
    def _empty_evidence(*, snapshot_id: str) -> ExecutableExitEvidence:
        return ExecutableExitEvidence(
            touch_bid=None,
            executable_bid=None,
            sweep_vwap=None,
            worst_price_to_fill=None,
            available_depth=Decimal("0"),
            available_depth_fraction=Decimal("0"),
            expected_slippage=None,
            book_age_ms=None,
            snapshot_id=snapshot_id,
            quality_verdict="unknown",
            spread=None,
            planner_evidence_ref=None,
        )


def planner_from_app(app) -> SurvivalExitPlanner:
    gate = None
    try:
        gate = build_quality_gate_from_config(app)
    except Exception:
        gate = DataQualityGate()
    max_age = None
    if app.runtime.market_data.enabled:
        max_age = float(app.runtime.market_data.max_book_age_s)
    return SurvivalExitPlanner(
        app.survival.exit_planning,
        quality_gate=gate,
        default_max_book_age_s=max_age,
    )
