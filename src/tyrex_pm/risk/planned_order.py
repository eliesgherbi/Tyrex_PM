"""Final planned-order validation (P3 architecture_enhance).

A dedicated validator that re-checks an :class:`ExecutionPlan` *after* the planner
has chosen the concrete style/price/size. It does NOT re-enter
``RiskEngine.evaluate_intent`` (that would re-mint a client order id and break
correlation). It re-runs only the gates that a planner can move:

* notional bounds (no silent re-cap; an over-cap plan is a planner bug → deny)
* deployment caps
* capital (BUY)
* inventory (SELL/Reduce)
* venue minimum size (deny; the validator never resizes a finalized plan)
* price-worsening guard (planner must not worsen the pre-check price; urgent
  exits are exempt because marketable pricing is the whole point)

On approval it returns a ``RiskDecision`` whose ``approved_intent`` reuses the
plan's ``client_order_id`` so the OMS submit keeps the same identity. The
emitted ``risk_decision`` fact carries ``{"phase": "planned"}``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import Side
from tyrex_pm.core.models import (
    ApprovedIntent,
    EnterIntent,
    ExitIntent,
    ReduceIntent,
    RiskContext,
    RiskDecision,
    URGENCY_URGENT,
)
from tyrex_pm.execution.models import ExecutionPlan
from tyrex_pm.risk import capital, deployment, inventory, kill_switch, venue_min_size
from tyrex_pm.risk.deployment import RiskConfigCaps
from tyrex_pm.risk.exits import apply_reduce_only_mark_fallback
from tyrex_pm.runtime.config import AppConfig


def _deny(reason: str, ext: dict[str, Any], detail: str | None = None) -> RiskDecision:
    return RiskDecision(False, (reason,), None, detail, None, ext)


def validate_planned_order(
    plan: ExecutionPlan,
    ctx: RiskContext,
    *,
    app: AppConfig,
) -> RiskDecision:
    r = app.risk
    work = plan.intent
    ext: dict[str, Any] = {
        "phase": "planned",
        "planner_reason": plan.planner_reason,
        "execution_style": work.order_style.value,
        "planned_size": str(work.size),
        "planned_limit_price": str(work.limit_price) if work.limit_price is not None else None,
        "urgency": plan.urgency,
    }
    if plan.book_evidence:
        ext["book_evidence"] = plan.book_evidence

    ok, reason = kill_switch.check_kill_switch(enabled=r.kill_switch.enabled)
    if not ok:
        return _deny(reason or rc.KILL_SWITCH, ext)

    # --- notional (no silent re-cap) ---------------------------------------
    price = work.limit_price or Decimal("0")
    notional = work.size * price
    ext["planned_notional_usd"] = str(notional)
    if notional < r.notional.min_usd:
        return _deny(rc.NOTIONAL_BELOW_MIN, ext)
    if notional > r.notional.max_usd:
        if r.notional.max_policy == "cap":
            # The planner should have respected the cap; a plan above it is a bug.
            return _deny(rc.PLANNER_NOTIONAL_VIOLATION, ext)
        return _deny(rc.NOTIONAL_ABOVE_MAX, ext)

    # --- deployment caps ---------------------------------------------------
    caps = RiskConfigCaps(
        token_cap_usd=r.deployment.token_cap_usd,
        portfolio_cap_usd=r.deployment.portfolio_cap_usd,
    )
    ok_d, reason_d, dep_ev = deployment.evaluate_deployment_caps(caps, ctx, pending_intent=work)
    ext = {**ext, **dep_ev}
    if not ok_d:
        book_evidence = ext.get("book_evidence") if isinstance(ext.get("book_evidence"), dict) else plan.book_evidence
        if reason_d == rc.DEPLOYMENT_MARK_UNKNOWN:
            ok_fb, reason_fb, dep_fb, _ = apply_reduce_only_mark_fallback(
                caps,
                ctx,
                work,
                app,
                book_evidence=book_evidence or plan.book_evidence,
            )
            if ok_fb:
                ext = {**ext, **dep_fb}
            else:
                ext = {**ext, **dep_fb}
                return _deny(reason_fb or reason_d or rc.TOKEN_DEPLOYMENT_CAP, ext)
        else:
            return _deny(reason_d or rc.TOKEN_DEPLOYMENT_CAP, ext)

    # --- capital (BUY) -----------------------------------------------------
    if isinstance(work, EnterIntent):
        cap_eval = capital.evaluate_capital_buy(work, ctx, enabled=r.capital.enabled)
        ext = {**ext, **cap_eval.evidence}
        if not cap_eval.ok:
            return _deny(cap_eval.reason or rc.INSUFFICIENT_CAPITAL, ext)

    # --- inventory (SELL / Reduce) ----------------------------------------
    if isinstance(work, (ExitIntent, ReduceIntent)):
        ok_i, reason_i = inventory.check_inventory_sell(
            work, ctx, require_position=r.inventory.sell_requires_venue_position
        )
        if not ok_i:
            return _deny(reason_i or rc.NAKED_SELL, ext)

    # --- venue minimum size (deny only; never resize a finalized plan) -----
    vms = venue_min_size.evaluate_venue_min_size(work, r.venue_min_size, ctx)
    ext = {**ext, **vms.evidence}
    if not vms.ok:
        return _deny(vms.deny_reason or rc.BELOW_VENUE_MIN_SIZE, ext)
    if vms.intent is not work and vms.intent.size != work.size:
        # policy=bump would resize the plan; the validator must not silently
        # change a finalized order — fail closed.
        ext["planner_resize_blocked"] = True
        return _deny(rc.PLANNER_RESIZE_NOT_ALLOWED, ext)

    # --- price-worsening guard (urgent exits exempt) -----------------------
    if plan.urgency != URGENCY_URGENT and plan.reference_limit_price is not None and work.limit_price is not None:
        ref = plan.reference_limit_price
        worsened = (
            (work.side == Side.BUY and work.limit_price > ref)
            or (work.side == Side.SELL and work.limit_price < ref)
        )
        if worsened:
            ext["reference_limit_price"] = str(ref)
            return _deny(rc.PLANNER_PRICE_WORSENED, ext)

    approved_intent = ApprovedIntent(
        intent=work,
        client_order_id=plan.client_order_id,
        run_id=plan.run_id,
    )
    return RiskDecision(True, (rc.APPROVED,), approved_intent, None, None, ext)
