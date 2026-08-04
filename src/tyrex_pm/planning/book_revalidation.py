"""Pre-submit book revalidation (BS-9): version change triggers recompute, not auto-reject."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from tyrex_pm.market_data.book_health import SyncHealth
from tyrex_pm.market_data.book_view import BookView, ExecutableBookQuote
from tyrex_pm.planning.plan import ExecutionPlan, PlanFailReason


@dataclass(frozen=True, kw_only=True)
class BookEvidenceSnapshot:
    """Plan-carried book identity/economics captured at evaluation time."""

    binding_id: str
    token_id: str
    side: str
    book_version: int
    pair_version: tuple[int, int]
    limit_price: Decimal
    quantity: Decimal
    expected_notional: Decimal
    tick_size: Decimal | None
    min_order_size: Decimal | None
    edge: Decimal | None = None
    evidence: Mapping[str, Any] | None = None


@dataclass(frozen=True, kw_only=True)
class RevalidationResult:
    ok: bool
    reason: str | None
    fail_reason: PlanFailReason | None
    audited_version_change: bool
    recomputed_quote: ExecutableBookQuote | None
    evidence: dict[str, Any]


def extract_book_evidence(plan: ExecutionPlan) -> BookEvidenceSnapshot | None:
    raw = plan.evidence or {}
    binding_id = raw.get("binding_id")
    token_id = str(plan.token_id.value)
    if not binding_id:
        return None
    pair = raw.get("pair_version") or (raw.get("book_version_up"), raw.get("book_version_down"))
    if isinstance(pair, list):
        pair_t = (int(pair[0]), int(pair[1]))
    elif isinstance(pair, tuple) and len(pair) == 2:
        pair_t = (int(pair[0]), int(pair[1]))
    else:
        pair_t = (int(raw.get("book_version", 0)), int(raw.get("book_version", 0)))
    return BookEvidenceSnapshot(
        binding_id=str(binding_id),
        token_id=token_id,
        side=str(raw.get("side") or plan.side.value),
        book_version=int(raw.get("book_version", 0)),
        pair_version=pair_t,
        limit_price=plan.limit_price,
        quantity=plan.quantity,
        expected_notional=plan.expected_notional,
        tick_size=plan.tick_size,
        min_order_size=plan.min_order_size,
        edge=None if raw.get("edge") is None else Decimal(str(raw["edge"])),
        evidence=raw,
    )


def revalidate_plan_against_book_view(
    plan: ExecutionPlan,
    view: BookView,
    *,
    active_binding_id: str,
    price_tolerance: Decimal = Decimal("0.02"),
    min_edge: Decimal | None = None,
    fee_available: bool = True,
) -> RevalidationResult:
    """Compare plan evidence to current BookView; recompute on version change.

    Hard invalidators block. Irrelevant distant-level version bumps audit and
    revalidate without automatic reject when economics still pass.
    """
    evidence = extract_book_evidence(plan)
    if evidence is None:
        return RevalidationResult(
            ok=False,
            reason="missing_book_evidence",
            fail_reason=PlanFailReason.MISSING_BOOK,
            audited_version_change=False,
            recomputed_quote=None,
            evidence={"detail": "plan.evidence.binding_id required"},
        )

    if view.binding_id != active_binding_id or evidence.binding_id != active_binding_id:
        return RevalidationResult(
            ok=False,
            reason="active_binding_changed",
            fail_reason=PlanFailReason.MISSING_BOOK,
            audited_version_change=False,
            recomputed_quote=None,
            evidence={
                "plan_binding_id": evidence.binding_id,
                "view_binding_id": view.binding_id,
                "active_binding_id": active_binding_id,
            },
        )

    token = evidence.token_id
    if token not in {view.up.token_id, view.down.token_id}:
        return RevalidationResult(
            ok=False,
            reason="token_mismatch",
            fail_reason=PlanFailReason.MISSING_BOOK,
            audited_version_change=False,
            recomputed_quote=None,
            evidence={"token_id": token},
        )

    leg = view.up if token == view.up.token_id else view.down
    if leg.sync_health in {SyncHealth.UNINITIALIZED, SyncHealth.SYNCING}:
        return RevalidationResult(
            ok=False,
            reason="BOOK_SYNCING" if leg.sync_health is SyncHealth.SYNCING else "BOOK_UNAVAILABLE",
            fail_reason=PlanFailReason.MISSING_BOOK,
            audited_version_change=False,
            recomputed_quote=None,
            evidence={"sync_health": leg.sync_health.value},
        )
    if leg.sync_health is SyncHealth.STALE:
        return RevalidationResult(
            ok=False,
            reason="BOOK_STALE",
            fail_reason=PlanFailReason.MISSING_BOOK,
            audited_version_change=True,
            recomputed_quote=None,
            evidence={"sync_health": leg.sync_health.value},
        )
    if leg.sync_health is SyncHealth.DESYNCED:
        return RevalidationResult(
            ok=False,
            reason="BOOK_DESYNCED",
            fail_reason=PlanFailReason.MISSING_BOOK,
            audited_version_change=True,
            recomputed_quote=None,
            evidence={"sync_health": leg.sync_health.value},
        )

    version_changed = leg.book_version != evidence.book_version
    quote = view.executable_buy(
        up=(token == view.up.token_id),
        requested_shares=evidence.quantity,
    )
    if quote is None or quote.vwap is None:
        return RevalidationResult(
            ok=False,
            reason="VENUE_ASK_EXPLICITLY_EMPTY",
            fail_reason=PlanFailReason.ONE_SIDED_BOOK,
            audited_version_change=version_changed,
            recomputed_quote=quote,
            evidence={"book_version": leg.book_version},
        )
    if quote.shortfall > 0:
        return RevalidationResult(
            ok=False,
            reason="INSUFFICIENT_DEPTH",
            fail_reason=PlanFailReason.INSUFFICIENT_DEPTH,
            audited_version_change=version_changed,
            recomputed_quote=quote,
            evidence={
                "shortfall": str(quote.shortfall),
                "available_depth": str(quote.available_depth),
            },
        )

    if not fee_available:
        return RevalidationResult(
            ok=False,
            reason="FEE_UNAVAILABLE",
            fail_reason=PlanFailReason.RISK_NOT_APPROVED,
            audited_version_change=version_changed,
            recomputed_quote=quote,
            evidence={},
        )

    if evidence.tick_size is not None and leg.tick_size is not None:
        if leg.tick_size != evidence.tick_size:
            # Tick change requires revalidation of price alignment.
            pass
    if evidence.min_order_size is not None and leg.min_order_size is not None:
        if evidence.quantity < leg.min_order_size:
            return RevalidationResult(
                ok=False,
                reason="BELOW_MIN_SIZE",
                fail_reason=PlanFailReason.BELOW_MIN_SIZE,
                audited_version_change=version_changed,
                recomputed_quote=quote,
                evidence={"min_order_size": str(leg.min_order_size)},
            )

    worst = quote.worst_price if quote.worst_price is not None else quote.vwap
    if worst is None:
        return RevalidationResult(
            ok=False,
            reason="PRICE_UNAVAILABLE",
            fail_reason=PlanFailReason.PRICE_ZERO,
            audited_version_change=version_changed,
            recomputed_quote=quote,
            evidence={},
        )
    if abs(worst - evidence.limit_price) > price_tolerance:
        return RevalidationResult(
            ok=False,
            reason="price_outside_tolerance",
            fail_reason=PlanFailReason.MAX_PRICE_EXCEEDED,
            audited_version_change=version_changed,
            recomputed_quote=quote,
            evidence={
                "limit_price": str(evidence.limit_price),
                "worst_price": str(worst),
                "tolerance": str(price_tolerance),
            },
        )

    if min_edge is not None and evidence.edge is not None and evidence.edge < min_edge:
        return RevalidationResult(
            ok=False,
            reason="NO_ECONOMIC_EDGE",
            fail_reason=PlanFailReason.RISK_NOT_APPROVED,
            audited_version_change=version_changed,
            recomputed_quote=quote,
            evidence={"edge": str(evidence.edge), "min_edge": str(min_edge)},
        )

    return RevalidationResult(
        ok=True,
        reason=None,
        fail_reason=None,
        audited_version_change=version_changed,
        recomputed_quote=quote,
        evidence={
            "book_version_then": evidence.book_version,
            "book_version_now": leg.book_version,
            "vwap": str(quote.vwap),
            "worst_price": str(worst),
        },
    )
