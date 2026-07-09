"""Reduce-only emergency min-notional bypass (Phase 2 operational hardening)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tyrex_pm.core.enums import Side
from tyrex_pm.core.models import EnterIntent, ExitIntent, ReduceIntent, RiskContext
from tyrex_pm.risk import inventory
from tyrex_pm.risk.pretrade import estimate_notional

ALLOWED_REDUCE_ONLY_CONTEXTS = frozenset(
    {
        "entry_fill_timeout",
        "saga_abort_unwind",
        "urgent_exit",
        "manual_flatten",
        "shutdown_flatten",
    }
)

# Pair-entry abort / saga unwind reasons map to saga_abort_unwind.
SAGA_ABORT_CONTEXT_ALIASES = frozenset(
    {
        "entry_leg_blocked",
        "resting_not_allowed",
        "entry_style_mismatch",
        "asymmetric_entry_timeout",
        "resting_timeout",
        "entry_price_mismatch",
        "activation_unwind",
        "emergency_unwind",
    }
)

BYPASS_DENY_NOT_SELL = "not_sell"
BYPASS_DENY_CONTEXT = "context_not_allowed"
BYPASS_DENY_NO_POSITION = "no_owned_position"
BYPASS_DENY_SIZE = "size_exceeds_available"
BYPASS_DENY_INCREASE = "would_increase_exposure"
BYPASS_DENY_MISSING_EVIDENCE = "missing_position_evidence"

BYPASS_REASON_APPROVED = "reduce_only_emergency_exit"


@dataclass(frozen=True)
class ReduceOnlyBypassResult:
    allowed: bool
    deny_reason: str | None = None
    fact_payload: dict[str, Any] | None = None


def normalize_reduce_only_context(raw: str | None) -> str | None:
    if raw is None or str(raw).strip() == "":
        return None
    key = str(raw).strip()
    if key in ALLOWED_REDUCE_ONLY_CONTEXTS:
        return key
    if key in SAGA_ABORT_CONTEXT_ALIASES:
        return "saga_abort_unwind"
    return None


def resolve_reduce_only_context(
    *,
    explicit: str | None = None,
    paired_binary_reason: str | None = None,
) -> str | None:
    for raw in (explicit, paired_binary_reason):
        normalized = normalize_reduce_only_context(raw)
        if normalized is not None:
            return normalized
    return None


def resolve_context_attempted(
    *,
    reduce_only_context: str | None = None,
    intent_extensions: dict[str, Any] | None = None,
) -> bool:
    ext = intent_extensions or {}
    return (
        resolve_reduce_only_context(
            explicit=reduce_only_context or (str(ext.get("reduce_only_context")) if ext.get("reduce_only_context") else None),
            paired_binary_reason=str(ext.get("paired_binary_reason"))
            if ext.get("paired_binary_reason")
            else None,
        )
        is not None
    )


def _position_quantities(
    intent: ExitIntent | ReduceIntent,
    ctx: RiskContext,
    *,
    intent_extensions: dict[str, Any] | None,
) -> tuple[Decimal, Decimal, bool]:
    """Return (available_to_sell, owned_evidence_qty, has_position_evidence)."""
    positions = {p.token_id: p for p in ctx.wallet_positions}
    wallet_avail = inventory.available_to_sell(
        token_id=intent.token_id,
        positions=positions,
        in_flight=ctx.orders_in_flight_by_token,
    )
    wallet_owned = positions.get(intent.token_id)
    wallet_qty = wallet_owned.qty if wallet_owned is not None else Decimal("0")

    allocation_qty: Decimal | None = None
    ext = intent_extensions or {}
    sizing = ext.get("paired_binary_sizing")
    if isinstance(sizing, dict):
        raw_alloc = sizing.get("owner_allocation")
        if raw_alloc is not None and str(raw_alloc).strip() != "":
            allocation_qty = Decimal(str(raw_alloc))

    venue_raw = None
    if isinstance(sizing, dict):
        venue_raw = sizing.get("venue_available")
    venue_qty = Decimal(str(venue_raw)) if venue_raw is not None and str(venue_raw).strip() else None

    has_evidence = wallet_qty > 0 or (allocation_qty is not None and allocation_qty > 0)
    if venue_qty is not None and venue_qty > 0:
        has_evidence = True

    available = wallet_avail
    if allocation_qty is not None:
        available = min(available, allocation_qty) if wallet_avail > 0 else allocation_qty
    if venue_qty is not None:
        available = min(available, venue_qty) if available > 0 else venue_qty

    owned_evidence = max(wallet_qty, allocation_qty or Decimal("0"), venue_qty or Decimal("0"))
    return available, owned_evidence, has_evidence


def _book_fields(exit_book_evidence: dict[str, Any] | None) -> dict[str, Any]:
    if not exit_book_evidence:
        return {"source": None, "source_quality": None, "book_age_ms": None}
    return {
        "source": exit_book_evidence.get("source", "websocket"),
        "source_quality": exit_book_evidence.get("source_quality"),
        "book_age_ms": exit_book_evidence.get("book_age_ms"),
    }


def evaluate_reduce_only_min_notional_bypass(
    intent: EnterIntent | ExitIntent | ReduceIntent,
    ctx: RiskContext,
    *,
    min_usd: Decimal,
    reduce_only_context: str | None = None,
    intent_extensions: dict[str, Any] | None = None,
    exit_book_evidence: dict[str, Any] | None = None,
    decision_id: str | None = None,
) -> ReduceOnlyBypassResult:
    """Return whether ``notional_below_min`` may be bypassed for this intent."""
    if isinstance(intent, EnterIntent):
        return ReduceOnlyBypassResult(False, BYPASS_DENY_INCREASE)

    if intent.side != Side.SELL:
        return ReduceOnlyBypassResult(False, BYPASS_DENY_NOT_SELL)

    ext = intent_extensions or {}
    context = resolve_reduce_only_context(
        explicit=str(ext.get("reduce_only_context")) if ext.get("reduce_only_context") else reduce_only_context,
        paired_binary_reason=str(ext.get("paired_binary_reason"))
        if ext.get("paired_binary_reason")
        else None,
    )
    if context is None:
        return ReduceOnlyBypassResult(False, BYPASS_DENY_CONTEXT)

    notional = estimate_notional(intent)
    if notional >= min_usd:
        return ReduceOnlyBypassResult(False, None)

    available, owned_evidence, has_evidence = _position_quantities(
        intent, ctx, intent_extensions=intent_extensions
    )
    if not has_evidence:
        return ReduceOnlyBypassResult(False, BYPASS_DENY_MISSING_EVIDENCE)
    if owned_evidence <= 0:
        return ReduceOnlyBypassResult(False, BYPASS_DENY_NO_POSITION)
    if intent.size <= 0:
        return ReduceOnlyBypassResult(False, BYPASS_DENY_INCREASE)
    if intent.size > owned_evidence or intent.size > available:
        return ReduceOnlyBypassResult(False, BYPASS_DENY_SIZE)

    book = _book_fields(exit_book_evidence)
    payload: dict[str, Any] = {
        "decision_id": decision_id,
        "token_id": str(intent.token_id),
        "side": intent.side.value,
        "context": context,
        "requested_qty": str(intent.size),
        "available_qty": str(available),
        "limit_price": str(intent.limit_price) if intent.limit_price is not None else None,
        "estimated_notional": str(notional),
        "min_notional": str(min_usd),
        "reason": BYPASS_REASON_APPROVED,
        **book,
    }
    return ReduceOnlyBypassResult(True, fact_payload=payload)


def build_bypass_denied_fact(
    intent: EnterIntent | ExitIntent | ReduceIntent,
    *,
    min_usd: Decimal,
    deny_reason: str,
    reduce_only_context: str | None = None,
    intent_extensions: dict[str, Any] | None = None,
    exit_book_evidence: dict[str, Any] | None = None,
    decision_id: str | None = None,
) -> dict[str, Any]:
    ext = intent_extensions or {}
    context = resolve_reduce_only_context(
        explicit=str(ext.get("reduce_only_context")) if ext.get("reduce_only_context") else reduce_only_context,
        paired_binary_reason=str(ext.get("paired_binary_reason"))
        if ext.get("paired_binary_reason")
        else None,
    )
    notional = estimate_notional(intent) if not isinstance(intent, EnterIntent) else Decimal("0")
    book = _book_fields(exit_book_evidence)
    return {
        "decision_id": decision_id,
        "token_id": str(getattr(intent, "token_id", "")),
        "side": getattr(getattr(intent, "side", None), "value", None),
        "context": context,
        "requested_qty": str(getattr(intent, "size", "")),
        "available_qty": None,
        "limit_price": str(intent.limit_price)
        if getattr(intent, "limit_price", None) is not None
        else None,
        "estimated_notional": str(notional),
        "min_notional": str(min_usd),
        "reason": deny_reason,
        **book,
    }
