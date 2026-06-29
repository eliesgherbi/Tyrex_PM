"""Reduce-only urgent exit risk helpers (architecture_enhance Phase 4.6)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import ExitIntent, ReduceIntent, RiskContext, URGENCY_URGENT
from tyrex_pm.core.time import utc_now
from tyrex_pm.execution.models import ExecutionPlan
from tyrex_pm.risk import inventory
from tyrex_pm.runtime.config import AppConfig
from tyrex_pm.state.market_store import MarketStateStore


def is_reduce_only_sell(
    intent: ExitIntent | ReduceIntent,
    ctx: RiskContext,
) -> bool:
    """True when the SELL closes existing venue inventory without exceeding it."""
    if intent.side != Side.SELL:
        return False
    positions = {p.token_id: p for p in ctx.wallet_positions}
    avail = inventory.available_to_sell(
        token_id=intent.token_id,
        positions=positions,
        in_flight=ctx.orders_in_flight_by_token,
    )
    return avail > 0 and intent.size <= avail


def build_exit_book_evidence_for_intent(
    market_state: MarketStateStore | None,
    token_id: TokenId,
    *,
    max_book_age_s: float,
) -> dict[str, Any] | None:
    """Build planner-compatible book evidence from MarketStateStore for phase-1 risk."""
    if market_state is None:
        return None
    snap = market_state.snapshot(token_id)
    if snap is None or snap.best_bid is None or snap.best_bid <= 0:
        return None
    stale = market_state.is_stale(token_id, max_age_s=max_book_age_s)
    age_ms = int(max(0.0, (utc_now() - snap.ts).total_seconds()) * 1000)
    return {
        "best_bid": str(snap.best_bid),
        "bid": str(snap.best_bid),
        "stale": stale,
        "book_stale": stale,
        "book_age_ms": age_ms,
        "book_update_ts": snap.ts.isoformat(),
    }


def executable_bid_from_book_evidence(book_evidence: dict[str, Any] | None) -> Decimal | None:
    if not book_evidence:
        return None
    raw = book_evidence.get("best_bid") or book_evidence.get("bid")
    if raw is not None and str(raw).strip() != "":
        try:
            bid = Decimal(str(raw))
            if bid > 0:
                return bid
        except Exception:
            pass
    return None


def executable_bid_from_plan(plan: ExecutionPlan, ctx: RiskContext) -> Decimal | None:
    """Best executable bid for mark fallback: book evidence only."""
    return executable_bid_from_book_evidence(plan.book_evidence)


def _book_is_stale_for_fallback(book_evidence: dict[str, Any] | None) -> bool:
    if not book_evidence:
        return True
    stale = book_evidence.get("stale") or book_evidence.get("book_stale")
    return stale in (True, "true", "True", 1)


def _reduce_only_fallback_evidence(
    intent: ExitIntent | ReduceIntent,
    ctx: RiskContext,
    bid: Decimal,
    book_evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    positions = {p.token_id: p for p in ctx.wallet_positions}
    avail = inventory.available_to_sell(
        token_id=intent.token_id,
        positions=positions,
        in_flight=ctx.orders_in_flight_by_token,
    )
    evidence: dict[str, Any] = {
        "reduce_only": True,
        "mark_source": "executable_bid",
        "mark_fallback_bid": str(bid),
        "reason": rc.REDUCE_ONLY_EXIT_MARK_FALLBACK,
        "deployment_mark_missing": True,
        "venue_available": str(avail),
        "allocation_available": str(avail),
        "planned_size": str(intent.size),
        "final_size": str(intent.size),
    }
    if book_evidence:
        if book_evidence.get("book_age_ms") is not None:
            evidence["book_age_ms"] = book_evidence.get("book_age_ms")
        if book_evidence.get("book_update_ts") is not None:
            evidence["book_update_ts"] = book_evidence.get("book_update_ts")
    return evidence


def reduce_only_exit_mark_fallback_eligible_intent(
    intent: ExitIntent | ReduceIntent,
    ctx: RiskContext,
    app: AppConfig,
    *,
    book_evidence: dict[str, Any] | None,
) -> tuple[bool, dict[str, Any]]:
    """Phase-1 eligibility: urgent reduce-only SELL with fresh executable bid."""
    cfg = app.risk.exits
    if not cfg.allow_reduce_only_mark_fallback:
        return False, {}
    if intent.urgency != URGENCY_URGENT:
        return False, {}
    if intent.side != Side.SELL:
        return False, {}
    if not is_reduce_only_sell(intent, ctx):
        return False, {"mark_fallback_blocked": "not_reduce_only"}
    bid = executable_bid_from_book_evidence(book_evidence)
    if bid is None or bid <= 0:
        return False, {"mark_fallback_blocked": "no_executable_bid"}
    if cfg.require_fresh_book_for_mark_fallback and _book_is_stale_for_fallback(book_evidence):
        return False, {"mark_fallback_blocked": "stale_book"}
    return True, _reduce_only_fallback_evidence(intent, ctx, bid, book_evidence)


def reduce_only_exit_mark_fallback_eligible(
    plan: ExecutionPlan,
    ctx: RiskContext,
    app: AppConfig,
) -> tuple[bool, dict[str, Any]]:
    """Check whether an urgent reduce-only SELL may bypass deployment mark unknown."""
    work = plan.intent
    if plan.urgency != URGENCY_URGENT:
        return False, {}
    if not isinstance(work, (ExitIntent, ReduceIntent)) or work.side != Side.SELL:
        return False, {}
    return reduce_only_exit_mark_fallback_eligible_intent(
        work,
        ctx,
        app,
        book_evidence=plan.book_evidence,
    )


def augmented_mark_prices_for_token(
    token_id: TokenId,
    ctx: RiskContext,
    fallback_evidence: dict[str, Any],
) -> dict[TokenId, Decimal]:
    """Copy mark_prices and inject executable bid for the sell token when missing."""
    marks = dict(ctx.mark_prices)
    if token_id not in marks or marks[token_id] is None or marks[token_id] <= 0:
        bid_raw = fallback_evidence.get("mark_fallback_bid")
        if bid_raw is not None:
            marks[token_id] = Decimal(str(bid_raw))
    return marks


def augmented_mark_prices_for_reduce_only_exit(
    plan: ExecutionPlan,
    ctx: RiskContext,
    fallback_evidence: dict[str, Any],
) -> dict:
    """Copy mark_prices and inject executable bid for the sell token when missing."""
    return augmented_mark_prices_for_token(plan.intent.token_id, ctx, fallback_evidence)


def apply_reduce_only_mark_fallback(
    caps,
    ctx: RiskContext,
    work: ExitIntent | ReduceIntent,
    app: AppConfig,
    *,
    book_evidence: dict[str, Any] | None,
) -> tuple[bool, str | None, dict[str, Any], dict[str, Any]]:
    """Re-run deployment caps with bid-injected marks when eligible."""
    from tyrex_pm.risk import deployment

    eligible, fb_ev = reduce_only_exit_mark_fallback_eligible_intent(
        work, ctx, app, book_evidence=book_evidence
    )
    if not eligible:
        return False, None, {}, fb_ev
    aug_marks = augmented_mark_prices_for_token(work.token_id, ctx, fb_ev)
    ctx_fb = RiskContext(
        execution_mode=ctx.execution_mode,
        wallet_positions=ctx.wallet_positions,
        open_orders=ctx.open_orders,
        usdc_balance=ctx.usdc_balance,
        usdc_allowance=ctx.usdc_allowance,
        last_wallet_sync_ts=ctx.last_wallet_sync_ts,
        mark_prices=aug_marks,
        kill_switch=ctx.kill_switch,
        health_ok=ctx.health_ok,
        heartbeat_ok=ctx.heartbeat_ok,
        clob_session_ok=ctx.clob_session_ok,
        in_flight_order_count=ctx.in_flight_order_count,
        orders_in_flight_by_token=ctx.orders_in_flight_by_token,
        reconcile_drift=ctx.reconcile_drift,
        venue_truth_stale=ctx.venue_truth_stale,
        in_flight_buy_reservations=ctx.in_flight_buy_reservations,
        first_v2_sync_complete=ctx.first_v2_sync_complete,
        market_info=ctx.market_info,
    )
    ok_d2, reason_d2, dep_ev2 = deployment.evaluate_deployment_caps(
        caps, ctx_fb, pending_intent=work
    )
    merged = {**dep_ev2, **fb_ev, "deployment_mark_fallback_applied": ok_d2}
    return ok_d2, reason_d2, merged, fb_ev
