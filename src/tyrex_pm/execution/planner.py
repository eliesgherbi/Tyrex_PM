"""ExecutionPlanner (P3 architecture_enhance + M4 executable depth).

Centralizes *how* to trade once risk has approved *what* to trade. When
``use_executable_depth`` is enabled, FAK plans use :class:`ExecutableBookView`
worst/sweep prices instead of touch-only estimates. Each plan attempt may carry
:class:`PlannerEvidence` with a fresh ``snapshot_id``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.models import (
    ApprovedIntent,
    EnterIntent,
    ExitIntent,
    ReduceIntent,
    URGENCY_PASSIVE,
    URGENCY_URGENT,
)
from tyrex_pm.execution.models import ExecutionPlan, ExecutionPlanResult, restyle_intent
from tyrex_pm.market_data.executable_book import ExecutableBookView, PlannerEvidence
from tyrex_pm.market_data.models import MarketStateSnapshot
from tyrex_pm.market_data.quality import DataQualityGate, DataQualityReport, DecisionContext
from tyrex_pm.runtime.config import ExecutionPlannerConfig


class ExecutionPlanner:
    def __init__(
        self,
        cfg: ExecutionPlannerConfig,
        *,
        quality_gate: DataQualityGate | None = None,
    ) -> None:
        self._cfg = cfg
        self._quality_gate = quality_gate

    def plan(
        self,
        approved: ApprovedIntent,
        *,
        market_state: Any | None = None,
        now: datetime | None = None,
        captured_snapshot: MarketStateSnapshot | None = None,
        executable_view: ExecutableBookView | None = None,
        quality_report: DataQualityReport | None = None,
        planner_evidence: PlannerEvidence | None = None,
        decision_context: DecisionContext | None = None,
    ) -> ExecutionPlanResult:
        intent = approved.intent
        if isinstance(intent, EnterIntent):
            return self._plan_entry(
                approved,
                intent,
                market_state=market_state,
                now=now,
                captured_snapshot=captured_snapshot,
                executable_view=executable_view,
                quality_report=quality_report,
                planner_evidence=planner_evidence,
                decision_context=decision_context or DecisionContext.ENTRY,
            )
        if isinstance(intent, (ExitIntent, ReduceIntent)):
            urgency = getattr(intent, "urgency", "normal")
            if urgency == URGENCY_URGENT:
                return self._plan_urgent_exit(
                    approved,
                    intent,
                    market_state,
                    now,
                    captured_snapshot=captured_snapshot,
                    executable_view=executable_view,
                    quality_report=quality_report,
                    planner_evidence=planner_evidence,
                    decision_context=decision_context or DecisionContext.URGENT_EXIT,
                )
            return self._plan_passive_exit(approved, intent)
        return ExecutionPlanResult(
            approved=False, reason=rc.PLANNER_UNSUPPORTED_INTENT, plan=None, evidence={}
        )

    def _attach_evidence(
        self,
        evidence: dict[str, Any],
        *,
        planner_evidence: PlannerEvidence | None,
        quality_report: DataQualityReport | None,
    ) -> dict[str, Any]:
        out = dict(evidence)
        if planner_evidence is not None:
            out["planner_evidence"] = planner_evidence.to_payload()
        if quality_report is not None:
            out["quality_report"] = quality_report.to_payload()
        return out

    def _maybe_deny_quality(
        self,
        *,
        context: DecisionContext,
        quality_report: DataQualityReport | None,
        evidence: dict[str, Any],
    ) -> ExecutionPlanResult | None:
        if quality_report is None or self._quality_gate is None:
            return None
        if self._quality_gate.allows_decision(quality_report, context):
            return None
        return ExecutionPlanResult(
            approved=False,
            reason=rc.PLANNER_QUALITY_REJECT,
            plan=None,
            evidence=self._attach_evidence(
                {**evidence, "quality_reasons": list(quality_report.reasons)},
                planner_evidence=None,
                quality_report=quality_report,
            ),
        )

    def _resolve_worst_price(
        self,
        *,
        token,
        side: Side,
        size: Decimal,
        market_state: Any,
        captured_snapshot: MarketStateSnapshot | None,
        executable_view: ExecutableBookView | None,
    ) -> tuple[Decimal | None, ExecutableBookView | None]:
        if self._cfg.use_executable_depth and executable_view is not None:
            return executable_view.worst_price_to_fill or executable_view.sweep_vwap, executable_view
        if self._cfg.use_executable_depth and captured_snapshot is not None:
            view = ExecutableBookView.from_snapshot(captured_snapshot, side=side, size=size)
            return view.worst_price_to_fill or view.sweep_vwap, view
        worst = market_state.estimate_fill_price(token, side, size)
        if worst is None:
            worst = market_state.best_ask(token) if side == Side.BUY else market_state.best_bid(token)
        return worst, executable_view

    # --- entries -----------------------------------------------------------
    def _plan_entry(
        self,
        approved: ApprovedIntent,
        intent: EnterIntent,
        *,
        market_state: Any | None = None,
        now: datetime | None = None,
        captured_snapshot: MarketStateSnapshot | None = None,
        executable_view: ExecutableBookView | None = None,
        quality_report: DataQualityReport | None = None,
        planner_evidence: PlannerEvidence | None = None,
        decision_context: DecisionContext,
    ) -> ExecutionPlanResult:
        urgency = getattr(intent, "urgency", "normal")
        if intent.order_style in (OrderStyle.FAK, OrderStyle.FOK):
            return self._plan_marketable_entry(
                approved,
                intent,
                market_state,
                now,
                urgency=urgency,
                captured_snapshot=captured_snapshot,
                executable_view=executable_view,
                quality_report=quality_report,
                planner_evidence=planner_evidence,
                decision_context=decision_context,
            )
        reason = rc.PLANNER_PASSIVE_ENTRY if urgency == URGENCY_PASSIVE else rc.PLANNER_NORMAL_ENTRY
        if intent.limit_price is None:
            return ExecutionPlanResult(
                approved=False,
                reason=rc.PLANNER_MISSING_BOOK,
                plan=None,
                evidence={"detail": "entry requires a limit price for GTC"},
            )
        final = restyle_intent(intent, order_style=OrderStyle.GTC, limit_price=intent.limit_price)
        return self._approved_plan(
            approved,
            final,
            reason=reason,
            urgency=urgency,
            evidence=self._attach_evidence(
                {"execution_style": "GTC", "limit_price": str(intent.limit_price)},
                planner_evidence=planner_evidence,
                quality_report=quality_report,
            ),
        )

    def _plan_marketable_entry(
        self,
        approved: ApprovedIntent,
        intent: EnterIntent,
        market_state: Any | None,
        now: datetime | None,
        *,
        urgency: str,
        captured_snapshot: MarketStateSnapshot | None = None,
        executable_view: ExecutableBookView | None = None,
        quality_report: DataQualityReport | None = None,
        planner_evidence: PlannerEvidence | None = None,
        decision_context: DecisionContext,
    ) -> ExecutionPlanResult:
        evidence: dict[str, Any] = {"requested_execution_style": intent.order_style.value}
        if intent.limit_price is None:
            return ExecutionPlanResult(
                approved=False,
                reason=rc.PLANNER_MISSING_BOOK,
                plan=None,
                evidence={"detail": "marketable entry requires a limit price ceiling"},
            )
        if market_state is None:
            return ExecutionPlanResult(
                approved=False,
                reason=rc.PLANNER_NO_MARKET_DATA,
                plan=None,
                evidence=evidence,
            )
        token = intent.token_id
        snap = captured_snapshot or (
            market_state.capture(token, now=now) if hasattr(market_state, "capture") else None
        )
        if snap is None and market_state.snapshot(token) is None:
            return ExecutionPlanResult(
                approved=False,
                reason=rc.PLANNER_MISSING_BOOK,
                plan=None,
                evidence=evidence,
            )
        if quality_report is None and self._quality_gate is not None and snap is not None:
            quality_report = self._quality_gate.evaluate_snapshot(
                snap, context=decision_context, size=intent.size
            )
        denied = self._maybe_deny_quality(
            context=decision_context, quality_report=quality_report, evidence=evidence
        )
        if denied is not None:
            if planner_evidence is not None:
                denied = ExecutionPlanResult(
                    approved=False,
                    reason=denied.reason,
                    plan=None,
                    evidence=self._attach_evidence(
                        evidence, planner_evidence=planner_evidence, quality_report=quality_report
                    ),
                )
            return denied
        if snap is not None and market_state.is_stale(token, max_age_s=self._cfg.max_book_age_s, now=now):
            evidence["book_age_limit_s"] = self._cfg.max_book_age_s
            return ExecutionPlanResult(
                approved=False,
                reason=rc.PLANNER_STALE_BOOK,
                plan=None,
                evidence=self._attach_evidence(evidence, planner_evidence=planner_evidence, quality_report=quality_report),
            )
        worst, view = self._resolve_worst_price(
            token=token,
            side=intent.side,
            size=intent.size,
            market_state=market_state,
            captured_snapshot=snap,
            executable_view=executable_view,
        )
        if worst is None:
            return ExecutionPlanResult(
                approved=False,
                reason=rc.PLANNER_MISSING_BOOK,
                plan=None,
                evidence=evidence,
            )
        if worst > intent.limit_price:
            evidence.update(
                {
                    "worst_acceptable_price": str(worst),
                    "limit_price": str(intent.limit_price),
                }
            )
            return ExecutionPlanResult(
                approved=False,
                reason=rc.PLANNER_PRICE_WORSENED,
                plan=None,
                evidence=self._attach_evidence(evidence, planner_evidence=planner_evidence, quality_report=quality_report),
            )
        style = intent.order_style
        reason = (
            rc.PLANNER_PAIRED_ENTRY_FOK
            if style == OrderStyle.FOK
            else rc.PLANNER_PAIRED_ENTRY_FAK
        )
        evidence.update(
            {
                "execution_style": style.value,
                "best_bid": _s(market_state.best_bid(token)),
                "best_ask": _s(market_state.best_ask(token)),
                "worst_acceptable_price": str(worst),
                "limit_price": str(intent.limit_price),
                "estimated_slippage": _s(
                    market_state.estimate_slippage(token, intent.side, intent.size)
                ),
            }
        )
        if view is not None:
            evidence.update(
                {
                    "touch_price": _s(view.touch_price),
                    "sweep_vwap": _s(view.sweep_vwap),
                    "available_depth": str(view.available_depth),
                    "snapshot_id": view.snapshot_id,
                }
            )
        final = restyle_intent(intent, order_style=style, limit_price=worst)
        return self._approved_plan(
            approved,
            final,
            reason=reason,
            urgency=urgency,
            evidence=self._attach_evidence(
                evidence, planner_evidence=planner_evidence, quality_report=quality_report
            ),
        )

    # --- passive exit ------------------------------------------------------
    def _plan_passive_exit(
        self, approved: ApprovedIntent, intent: ExitIntent | ReduceIntent
    ) -> ExecutionPlanResult:
        if intent.limit_price is None:
            return ExecutionPlanResult(
                approved=False,
                reason=rc.PLANNER_MISSING_BOOK,
                plan=None,
                evidence={"detail": "passive exit requires a limit price for GTC"},
            )
        final = restyle_intent(intent, order_style=OrderStyle.GTC, limit_price=intent.limit_price)
        return self._approved_plan(
            approved,
            final,
            reason=rc.PLANNER_PASSIVE_EXIT,
            urgency=getattr(intent, "urgency", "normal"),
            evidence={"execution_style": "GTC", "limit_price": str(intent.limit_price)},
        )

    # --- urgent / protection exit -----------------------------------------
    def _plan_urgent_exit(
        self,
        approved: ApprovedIntent,
        intent: ExitIntent | ReduceIntent,
        market_state: Any | None,
        now: datetime | None,
        *,
        captured_snapshot: MarketStateSnapshot | None = None,
        executable_view: ExecutableBookView | None = None,
        quality_report: DataQualityReport | None = None,
        planner_evidence: PlannerEvidence | None = None,
        decision_context: DecisionContext,
    ) -> ExecutionPlanResult:
        evidence: dict[str, Any] = {"urgency": URGENCY_URGENT}

        def _fallback_or_deny(reason: str) -> ExecutionPlanResult:
            if self._cfg.allow_urgent_exit_fallback and intent.limit_price is not None:
                final = restyle_intent(
                    intent, order_style=OrderStyle.FAK, limit_price=intent.limit_price
                )
                ev = {**evidence, "fallback_limit_price": str(intent.limit_price), "deny_reason": reason}
                return self._approved_plan(
                    approved,
                    final,
                    reason=rc.PLANNER_URGENT_EXIT_FALLBACK,
                    urgency=URGENCY_URGENT,
                    evidence=self._attach_evidence(
                        ev, planner_evidence=planner_evidence, quality_report=quality_report
                    ),
                )
            return ExecutionPlanResult(
                approved=False,
                reason=reason,
                plan=None,
                evidence=self._attach_evidence(
                    evidence, planner_evidence=planner_evidence, quality_report=quality_report
                ),
            )

        if market_state is None:
            return _fallback_or_deny(rc.PLANNER_NO_MARKET_DATA)

        token = intent.token_id
        snap = captured_snapshot or (
            market_state.capture(token, now=now) if hasattr(market_state, "capture") else None
        )
        if snap is None and market_state.snapshot(token) is None:
            return _fallback_or_deny(rc.PLANNER_MISSING_BOOK)
        if quality_report is None and self._quality_gate is not None and snap is not None:
            quality_report = self._quality_gate.evaluate_snapshot(
                snap, context=decision_context, size=intent.size
            )
        denied = self._maybe_deny_quality(
            context=decision_context, quality_report=quality_report, evidence=evidence
        )
        if denied is not None:
            return ExecutionPlanResult(
                approved=False,
                reason=denied.reason,
                plan=None,
                evidence=self._attach_evidence(
                    evidence, planner_evidence=planner_evidence, quality_report=quality_report
                ),
            )
        if snap is not None and market_state.is_stale(token, max_age_s=self._cfg.max_book_age_s, now=now):
            evidence["book_age_limit_s"] = self._cfg.max_book_age_s
            return _fallback_or_deny(rc.PLANNER_STALE_BOOK)

        side = intent.side
        worst, view = self._resolve_worst_price(
            token=token,
            side=side,
            size=intent.size,
            market_state=market_state,
            captured_snapshot=snap,
            executable_view=executable_view,
        )
        if worst is None:
            return _fallback_or_deny(rc.PLANNER_MISSING_BOOK)

        evidence.update(
            {
                "execution_style": "FAK",
                "best_bid": _s(market_state.best_bid(token)),
                "best_ask": _s(market_state.best_ask(token)),
                "worst_acceptable_price": str(worst),
                "estimated_slippage": _s(market_state.estimate_slippage(token, side, intent.size)),
            }
        )
        if view is not None:
            evidence.update(
                {
                    "touch_price": _s(view.touch_price),
                    "sweep_vwap": _s(view.sweep_vwap),
                    "available_depth": str(view.available_depth),
                    "snapshot_id": view.snapshot_id,
                    "quality_verdict": quality_report.verdict.value if quality_report else None,
                }
            )
        final = restyle_intent(intent, order_style=OrderStyle.FAK, limit_price=worst)
        return self._approved_plan(
            approved,
            final,
            reason=rc.PLANNER_URGENT_EXIT_FAK,
            urgency=URGENCY_URGENT,
            evidence=self._attach_evidence(
                evidence, planner_evidence=planner_evidence, quality_report=quality_report
            ),
        )

    # --- helpers -----------------------------------------------------------
    def _approved_plan(
        self,
        approved: ApprovedIntent,
        final_intent,
        *,
        reason: str,
        urgency: str,
        evidence: dict[str, Any],
    ) -> ExecutionPlanResult:
        plan = ExecutionPlan(
            intent=final_intent,
            client_order_id=approved.client_order_id,
            run_id=approved.run_id,
            planner_reason=reason,
            urgency=urgency,
            reference_limit_price=approved.intent.limit_price,
            book_evidence=evidence,
        )
        return ExecutionPlanResult(approved=True, reason=reason, plan=plan, evidence=evidence)


def _s(v: Decimal | None) -> str | None:
    return str(v) if v is not None else None


def build_quality_gate_from_config(app_cfg) -> DataQualityGate:
    from decimal import Decimal as D

    from tyrex_pm.market_data.quality import (
        CRYPTO_5M_PROFILE,
        DataQualityGate,
        DataQualityGateConfig,
        MarketProfileThresholds,
    )

    md = app_cfg.runtime.market_data
    q = md.quality
    profiles: dict[str, MarketProfileThresholds] = {"crypto_5m": CRYPTO_5M_PROFILE}
    if md.market_profiles:
        for name, raw in md.market_profiles.items():
            if not isinstance(raw, dict):
                continue
            profiles[name] = MarketProfileThresholds(
                pass_max_age_ms=int(raw.get("pass_max_age_ms", 750)),
                reject_max_age_ms=int(raw.get("reject_max_age_ms", 1500)),
                emergency_max_age_ms=int(raw.get("emergency_max_age_ms", 3000)),
                max_spread=D(str(raw.get("max_spread", "0.15"))),
                min_depth_at_size=D(str(raw.get("min_depth_at_size", "5"))),
                require_external_price=bool(raw.get("require_external_price", False)),
            )
    return DataQualityGate(
        DataQualityGateConfig(
            enforcement_mode=q.enforcement_mode,
            market_profile=q.market_profile,
            require_ws_primary_for_entry=q.require_ws_primary_for_entry,
            allow_rest_recovery_for_exit=q.allow_rest_recovery_for_exit,
            allow_rest_recovery_for_entry=q.allow_rest_recovery_for_entry,
            profiles=profiles,
        )
    )
