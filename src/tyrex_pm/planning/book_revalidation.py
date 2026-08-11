"""Pre-submit book revalidation over the newest authoritative book view.

Version change triggers recompute, not automatic reject. Hard invalidators
(binding/window, sync, freshness, depth, edge, fee cap, cutoff) refuse before
mutation. Latency budget misses are warnings only (D-06).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    window_slug: str | None = None
    market_id: str | None = None
    book_fingerprint: str | None = None
    candidate_mono_ns: int | None = None
    evidence: Mapping[str, Any] | None = None


@dataclass(frozen=True, kw_only=True)
class RevalidationResult:
    ok: bool
    reason: str | None
    fail_reason: PlanFailReason | None
    audited_version_change: bool
    recomputed_quote: ExecutableBookQuote | None
    evidence: dict[str, Any]
    warning: str | None = None


@dataclass(frozen=True, kw_only=True)
class PresubmitRevalidationArtifact:
    """Immutable FRH-07 audit record for one pre-submit revalidation."""

    ok: bool
    reason: str | None
    book_fingerprint: str
    book_version: int
    pair_version: tuple[int, int]
    binding_id: str
    window_slug: str | None
    candidate_age_ms: float | None
    audited_version_change: bool
    fee_inclusive_debit: str | None
    fee_cap: str | None
    edge: str | None
    min_edge: str | None
    latency_warning: str | None
    checked_at_mono_ns: int
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "book_fingerprint": self.book_fingerprint,
            "book_version": self.book_version,
            "pair_version": list(self.pair_version),
            "binding_id": self.binding_id,
            "window_slug": self.window_slug,
            "candidate_age_ms": self.candidate_age_ms,
            "audited_version_change": self.audited_version_change,
            "fee_inclusive_debit": self.fee_inclusive_debit,
            "fee_cap": self.fee_cap,
            "edge": self.edge,
            "min_edge": self.min_edge,
            "latency_warning": self.latency_warning,
            "checked_at_mono_ns": self.checked_at_mono_ns,
            "evidence": dict(self.evidence),
        }


def book_view_fingerprint(view: BookView, *, token_id: str | None = None) -> str:
    """Stable fingerprint of the executable book levels used for submission."""
    legs = []
    for leg in (view.up, view.down):
        if token_id is not None and leg.token_id != token_id:
            continue
        book = leg.book
        legs.append(
            {
                "token_id": leg.token_id,
                "book_version": leg.book_version,
                "sync_health": leg.sync_health.value,
                "bids": (
                    []
                    if book is None
                    else [[str(level.price), str(level.quantity)] for level in book.bids[:10]]
                ),
                "asks": (
                    []
                    if book is None
                    else [[str(level.price), str(level.quantity)] for level in book.asks[:10]]
                ),
            }
        )
    payload = {
        "binding_id": view.binding_id,
        "window_slug": view.window_slug,
        "pair_version": list(view.pair_version),
        "legs": legs,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


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
    cand_mono = raw.get("candidate_mono_ns")
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
        window_slug=None if raw.get("window_slug") is None else str(raw["window_slug"]),
        market_id=None if raw.get("market_id") is None else str(raw["market_id"]),
        book_fingerprint=(
            None if raw.get("book_fingerprint") is None else str(raw["book_fingerprint"])
        ),
        candidate_mono_ns=None if cand_mono is None else int(cand_mono),
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
    expected_window_slug: str | None = None,
    expected_market_id: str | None = None,
    now: datetime | None = None,
    entry_allowed_until: datetime | None = None,
    max_book_age_ms: int | None = None,
    max_candidate_age_ms: int | None = None,
    now_mono_ns: int | None = None,
    fee_inclusive_debit: Decimal | None = None,
    fee_cap: Decimal | None = None,
    max_spread: Decimal | None = None,
    latency_budget_ms: int | None = None,
) -> RevalidationResult:
    """Compare plan evidence to current BookView; recompute on version change.

    Hard invalidators block. Irrelevant distant-level version bumps audit and
    revalidate without automatic reject when economics still pass.
    """
    mono = time.monotonic_ns() if now_mono_ns is None else now_mono_ns
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

    exp_window = expected_window_slug or evidence.window_slug
    if exp_window is not None and view.window_slug != exp_window:
        return RevalidationResult(
            ok=False,
            reason="window_changed",
            fail_reason=PlanFailReason.MISSING_BOOK,
            audited_version_change=False,
            recomputed_quote=None,
            evidence={
                "expected_window_slug": exp_window,
                "view_window_slug": view.window_slug,
            },
        )

    exp_market = expected_market_id or evidence.market_id
    if exp_market is not None and view.condition_id and view.condition_id != exp_market:
        # condition_id is the market/condition binding key on BookView.
        if evidence.market_id is not None and evidence.market_id != view.condition_id:
            return RevalidationResult(
                ok=False,
                reason="market_binding_changed",
                fail_reason=PlanFailReason.MISSING_BOOK,
                audited_version_change=False,
                recomputed_quote=None,
                evidence={
                    "expected_market_id": exp_market,
                    "view_condition_id": view.condition_id,
                },
            )

    wall = now or datetime.now(timezone.utc)
    if entry_allowed_until is not None and wall >= entry_allowed_until:
        return RevalidationResult(
            ok=False,
            reason="entry_cutoff_passed",
            fail_reason=PlanFailReason.RISK_NOT_APPROVED,
            audited_version_change=False,
            recomputed_quote=None,
            evidence={
                "now": wall.isoformat(),
                "entry_allowed_until": entry_allowed_until.isoformat(),
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

    if max_book_age_ms is not None and leg.data_age_ms is not None:
        if leg.data_age_ms > max_book_age_ms:
            return RevalidationResult(
                ok=False,
                reason="BOOK_FRESHNESS",
                fail_reason=PlanFailReason.MISSING_BOOK,
                audited_version_change=False,
                recomputed_quote=None,
                evidence={
                    "data_age_ms": leg.data_age_ms,
                    "max_book_age_ms": max_book_age_ms,
                },
            )

    candidate_age_ms: float | None = None
    if evidence.candidate_mono_ns is not None:
        candidate_age_ms = (mono - evidence.candidate_mono_ns) / 1_000_000.0
        if max_candidate_age_ms is not None and candidate_age_ms > max_candidate_age_ms:
            return RevalidationResult(
                ok=False,
                reason="stale_candidate",
                fail_reason=PlanFailReason.RISK_NOT_APPROVED,
                audited_version_change=False,
                recomputed_quote=None,
                evidence={
                    "candidate_age_ms": candidate_age_ms,
                    "max_candidate_age_ms": max_candidate_age_ms,
                },
            )

    latency_warning: str | None = None
    if (
        latency_budget_ms is not None
        and candidate_age_ms is not None
        and candidate_age_ms > latency_budget_ms
    ):
        # D-06: latency budget miss is a warning, not an automatic hard refuse.
        latency_warning = "latency_budget_miss"

    if max_spread is not None and leg.book is not None:
        best_ask = leg.quote.best_ask
        best_bid = leg.quote.best_bid
        if best_ask is not None and best_bid is not None:
            spread = best_ask - best_bid
            if spread > max_spread:
                return RevalidationResult(
                    ok=False,
                    reason="spread_gate",
                    fail_reason=PlanFailReason.RISK_NOT_APPROVED,
                    audited_version_change=False,
                    recomputed_quote=None,
                    evidence={
                        "spread": str(spread),
                        "max_spread": str(max_spread),
                    },
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

    if fee_inclusive_debit is not None and fee_cap is not None:
        if fee_inclusive_debit > fee_cap:
            return RevalidationResult(
                ok=False,
                reason="fee_inclusive_exceeds_cap",
                fail_reason=PlanFailReason.RISK_NOT_APPROVED,
                audited_version_change=version_changed,
                recomputed_quote=quote,
                evidence={
                    "fee_inclusive_debit": str(fee_inclusive_debit),
                    "fee_cap": str(fee_cap),
                },
            )

    fp = book_view_fingerprint(view, token_id=token)
    return RevalidationResult(
        ok=True,
        reason=None,
        fail_reason=None,
        audited_version_change=version_changed,
        recomputed_quote=quote,
        warning=latency_warning,
        evidence={
            "book_version_then": evidence.book_version,
            "book_version_now": leg.book_version,
            "pair_version_now": list(view.pair_version),
            "vwap": str(quote.vwap),
            "worst_price": str(worst),
            "book_fingerprint": fp,
            "candidate_age_ms": candidate_age_ms,
            "window_slug": view.window_slug,
        },
    )


def build_presubmit_artifact(
    *,
    result: RevalidationResult,
    view: BookView,
    plan: ExecutionPlan,
    fee_inclusive_debit: Decimal | None = None,
    fee_cap: Decimal | None = None,
    min_edge: Decimal | None = None,
    now_mono_ns: int | None = None,
) -> PresubmitRevalidationArtifact:
    """FRH-07: immutable revalidation audit payload."""
    mono = time.monotonic_ns() if now_mono_ns is None else now_mono_ns
    evidence = extract_book_evidence(plan)
    token = str(plan.token_id.value)
    leg = view.up if token == view.up.token_id else view.down
    fp = str(result.evidence.get("book_fingerprint") or book_view_fingerprint(view, token_id=token))
    age = result.evidence.get("candidate_age_ms")
    edge = None if evidence is None or evidence.edge is None else str(evidence.edge)
    return PresubmitRevalidationArtifact(
        ok=result.ok,
        reason=result.reason,
        book_fingerprint=fp,
        book_version=leg.book_version,
        pair_version=view.pair_version,
        binding_id=view.binding_id,
        window_slug=view.window_slug,
        candidate_age_ms=None if age is None else float(age),
        audited_version_change=result.audited_version_change,
        fee_inclusive_debit=None if fee_inclusive_debit is None else str(fee_inclusive_debit),
        fee_cap=None if fee_cap is None else str(fee_cap),
        edge=edge,
        min_edge=None if min_edge is None else str(min_edge),
        latency_warning=result.warning,
        checked_at_mono_ns=mono,
        evidence=dict(result.evidence),
    )
