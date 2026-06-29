"""ExecutionPlanner (P3 architecture_enhance).

Centralizes *how* to trade once risk has approved *what* to trade. It converts a
pre-check ``ApprovedIntent`` into a concrete :class:`ExecutionPlan` (order style,
price, size) using shared market-state truth.

Initial rule set (deliberately small — GTD/post-only/slicing are future work):

* passive entry            → GTC at the strategy limit price
* normal entry             → GTC at the strategy limit price (when intent style is GTC)
* FAK/FOK entry            → marketable at book-derived worst acceptable price (paired entry)
* passive exit             → GTC at the strategy limit price
* urgent/protection exit   → FAK at a marketable worst-acceptable price from the book
* urgent exit, stale/missing book → deny (fail closed) unless an explicit fallback
  limit is configured

Market-data activation: passive/normal entries do not need a fresh book (the
strategy limit price is authoritative). Urgent/protection exits require a fresh
book unless ``allow_urgent_exit_fallback`` is set and the intent carries a limit.
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
from tyrex_pm.runtime.config import ExecutionPlannerConfig


class ExecutionPlanner:
    def __init__(self, cfg: ExecutionPlannerConfig) -> None:
        self._cfg = cfg

    def plan(
        self,
        approved: ApprovedIntent,
        *,
        market_state: Any | None = None,
        now: datetime | None = None,
    ) -> ExecutionPlanResult:
        intent = approved.intent
        if isinstance(intent, EnterIntent):
            return self._plan_entry(approved, intent, market_state=market_state, now=now)
        if isinstance(intent, (ExitIntent, ReduceIntent)):
            urgency = getattr(intent, "urgency", "normal")
            if urgency == URGENCY_URGENT:
                return self._plan_urgent_exit(approved, intent, market_state, now)
            return self._plan_passive_exit(approved, intent)
        return ExecutionPlanResult(
            approved=False, reason=rc.PLANNER_UNSUPPORTED_INTENT, plan=None, evidence={}
        )

    # --- entries -----------------------------------------------------------
    def _plan_entry(
        self,
        approved: ApprovedIntent,
        intent: EnterIntent,
        *,
        market_state: Any | None = None,
        now: datetime | None = None,
    ) -> ExecutionPlanResult:
        urgency = getattr(intent, "urgency", "normal")
        if intent.order_style in (OrderStyle.FAK, OrderStyle.FOK):
            return self._plan_marketable_entry(
                approved,
                intent,
                market_state,
                now,
                urgency=urgency,
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
        return self._approved_plan(approved, final, reason=reason, urgency=urgency, evidence={
            "execution_style": "GTC",
            "limit_price": str(intent.limit_price),
        })

    def _plan_marketable_entry(
        self,
        approved: ApprovedIntent,
        intent: EnterIntent,
        market_state: Any | None,
        now: datetime | None,
        *,
        urgency: str,
    ) -> ExecutionPlanResult:
        """FAK/FOK paired entry: marketable BUY at worst acceptable ask-side price."""
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
        if market_state.snapshot(token) is None:
            return ExecutionPlanResult(
                approved=False,
                reason=rc.PLANNER_MISSING_BOOK,
                plan=None,
                evidence=evidence,
            )
        if market_state.is_stale(token, max_age_s=self._cfg.max_book_age_s, now=now):
            evidence["book_age_limit_s"] = self._cfg.max_book_age_s
            return ExecutionPlanResult(
                approved=False,
                reason=rc.PLANNER_STALE_BOOK,
                plan=None,
                evidence=evidence,
            )
        worst = market_state.estimate_fill_price(token, intent.side, intent.size)
        if worst is None:
            worst = market_state.best_ask(token)
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
                evidence=evidence,
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
        final = restyle_intent(intent, order_style=style, limit_price=worst)
        return self._approved_plan(approved, final, reason=reason, urgency=urgency, evidence=evidence)

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
    ) -> ExecutionPlanResult:
        evidence: dict[str, Any] = {"urgency": URGENCY_URGENT}

        def _fallback_or_deny(reason: str) -> ExecutionPlanResult:
            if self._cfg.allow_urgent_exit_fallback and intent.limit_price is not None:
                final = restyle_intent(
                    intent, order_style=OrderStyle.FAK, limit_price=intent.limit_price
                )
                ev = {**evidence, "fallback_limit_price": str(intent.limit_price), "deny_reason": reason}
                return self._approved_plan(
                    approved, final, reason=rc.PLANNER_URGENT_EXIT_FALLBACK,
                    urgency=URGENCY_URGENT, evidence=ev,
                )
            return ExecutionPlanResult(approved=False, reason=reason, plan=None, evidence=evidence)

        if market_state is None:
            return _fallback_or_deny(rc.PLANNER_NO_MARKET_DATA)

        token = intent.token_id
        if market_state.snapshot(token) is None:
            return _fallback_or_deny(rc.PLANNER_MISSING_BOOK)
        if market_state.is_stale(token, max_age_s=self._cfg.max_book_age_s, now=now):
            evidence["book_age_limit_s"] = self._cfg.max_book_age_s
            return _fallback_or_deny(rc.PLANNER_STALE_BOOK)

        # Worst-acceptable marketable price: the VWAP across the resting book for
        # this size (<= best bid for a SELL). FAK fills what it can at/above this
        # floor and cancels the rest.
        side = intent.side
        worst = market_state.estimate_fill_price(token, side, intent.size)
        if worst is None:
            best = market_state.best_bid(token) if side == Side.SELL else market_state.best_ask(token)
            worst = best
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
        final = restyle_intent(intent, order_style=OrderStyle.FAK, limit_price=worst)
        return self._approved_plan(
            approved, final, reason=rc.PLANNER_URGENT_EXIT_FAK, urgency=URGENCY_URGENT, evidence=evidence
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
