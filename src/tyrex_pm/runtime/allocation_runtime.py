"""Runtime wiring for the allocation ledger (P4): mutations, facts, owner resolution."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import ApprovedIntent, EnterIntent, ExitIntent, Intent, ReduceIntent
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_ALLOCATION_LEDGER
from tyrex_pm.runtime.allocation_ids import (
    OWNER_GURU_FOLLOW,
    OWNER_SELL_TEST,
    SCHEDULED_EXIT_DEMO_SOURCE,
    SELL_TEST_INTENT_SOURCE,
)
from tyrex_pm.runtime.config import AppConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.exit_lifecycle import parse_taking_amount, oms_status_is_matched
from tyrex_pm.state.allocation_ledger import AllocationClampResult, AllocationLedger, AllocationMutation



def resolve_owner_id(
    strategy: object,
    intent: Intent,
    *,
    intent_extensions: dict[str, Any] | None = None,
) -> str:
    ext = intent_extensions or {}
    owner = ext.get("allocation_owner_id")
    if owner is not None and str(owner).strip():
        return str(owner).strip()
    if type(strategy).__name__ == "SellTestStrategy":
        return OWNER_SELL_TEST
    source = ext.get("source")
    if source == SELL_TEST_INTENT_SOURCE:
        return OWNER_SELL_TEST
    if source == SCHEDULED_EXIT_DEMO_SOURCE:
        return OWNER_GURU_FOLLOW
    return OWNER_GURU_FOLLOW


def fill_qty_for_allocation(
    intent: Intent,
    match_evidence: dict[str, Any],
    approved_size: Decimal,
) -> Decimal:
    """Best-effort filled share qty from OMS match evidence or approved intent size."""
    if isinstance(intent, EnterIntent) and intent.side == Side.BUY:
        taking = parse_taking_amount(match_evidence)
        if taking is not None:
            return min(approved_size, taking)
        return approved_size
    if isinstance(intent, (ExitIntent, ReduceIntent)) and intent.side == Side.SELL:
        making_raw = match_evidence.get("making_amount")
        if making_raw is not None and str(making_raw).strip() != "":
            try:
                making = Decimal(str(making_raw))
            except Exception:
                making = None
            if making is not None and making > 0:
                return min(approved_size, making)
        return approved_size
    return approved_size


def clamp_planned_to_allocated(
    coord: RuntimeCoordinator,
    *,
    owner_id: str,
    token_id: TokenId,
    planned: Decimal,
) -> Decimal:
    ledger = coord.allocation_ledger
    if ledger is None:
        return Decimal("0")
    avail = ledger.get_available_allocated(owner_id, token_id)
    if avail <= 0:
        return Decimal("0")
    return min(planned, avail)


def _ledger_balance_fields(
    ledger: AllocationLedger,
    owner_id: str,
    token_id: str | TokenId,
) -> dict[str, str]:
    return {
        "allocated_qty": str(ledger.get_allocated(owner_id, token_id)),
        "reserved_exit_qty": str(ledger.get_reserved(owner_id, token_id)),
        "available_allocated": str(ledger.get_available_allocated(owner_id, token_id)),
    }


def _emit_allocation_fact(
    coord: RuntimeCoordinator,
    run_id: str,
    mutation: AllocationMutation | AllocationClampResult,
    *,
    event: str | None = None,
    correlation_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    if coord.allocation_ledger_sink is None or coord.allocation_ledger_run_id is None:
        return
    if isinstance(mutation, AllocationClampResult):
        payload: dict[str, Any] = {
            "event": event or "allocation_clamped",
            "owner_id": mutation.owner_id,
            "token_id": mutation.token_id,
            "allocated_before": str(mutation.allocated_before),
            "allocated_after": str(mutation.allocated_after),
            "venue_qty": str(mutation.venue_qty),
            "delta_qty": str(mutation.allocated_after - mutation.allocated_before),
        }
    else:
        payload = {
            "event": event or mutation.event,
            "owner_id": mutation.owner_id,
            "token_id": mutation.token_id,
            "delta_qty": str(mutation.delta_qty),
            "allocated_before": str(mutation.allocated_before),
            "allocated_after": str(mutation.allocated_after),
        }
        if mutation.correlation_id is not None:
            payload["correlation_id"] = mutation.correlation_id
        if mutation.reservation_id is not None:
            payload["reservation_id"] = mutation.reservation_id
        if mutation.venue_qty is not None:
            payload["venue_qty"] = str(mutation.venue_qty)
        if mutation.source is not None:
            payload["source"] = mutation.source
        if mutation.reason is not None:
            payload["reason"] = mutation.reason
        if mutation.filled_qty is not None:
            payload["filled_qty"] = str(mutation.filled_qty)
        if mutation.reserved_before is not None:
            payload["reserved_before"] = str(mutation.reserved_before)
        if mutation.reserved_after is not None:
            payload["reserved_after"] = str(mutation.reserved_after)
        if mutation.venue_order_id is not None:
            payload["venue_order_id"] = mutation.venue_order_id
        if mutation.partial:
            payload["partial"] = True
    ledger = coord.allocation_ledger
    if ledger is not None and isinstance(mutation, AllocationMutation):
        payload.update(_ledger_balance_fields(ledger, mutation.owner_id, mutation.token_id))
    if extra:
        payload.update(extra)
    corr = correlation_id or getattr(mutation, "correlation_id", None)
    coord.allocation_ledger_sink.write(
        make_fact(
            FACT_TYPE_ALLOCATION_LEDGER,
            coord.allocation_ledger_run_id,
            payload,
            correlation_id=corr,
        )
    )


def maybe_apply_allocation_buy(
    coord: RuntimeCoordinator,
    app: AppConfig,
    *,
    strategy: object,
    ap: ApprovedIntent,
    match_evidence: dict[str, Any],
    correlation_id: str,
    intent_extensions: dict[str, Any] | None,
    run_id: str,
) -> None:
    ledger = coord.allocation_ledger
    if ledger is None:
        return
    intent = ap.intent
    if not isinstance(intent, EnterIntent) or intent.side != Side.BUY:
        return
    owner_id = resolve_owner_id(strategy, intent, intent_extensions=intent_extensions)
    qty = fill_qty_for_allocation(intent, match_evidence, intent.size)
    mut = ledger.apply_buy(owner_id, intent.token_id, qty, correlation_id=correlation_id)
    _emit_allocation_fact(coord, run_id, mut, correlation_id=correlation_id)


def should_apply_allocation_sell_on_submit(
    match_evidence: dict[str, Any],
    *,
    apply_local_shadow_fill: bool,
) -> bool:
    """Apply allocation decrement only on matched fills (or shadow instant fill)."""
    if apply_local_shadow_fill:
        return True
    return oms_status_is_matched(match_evidence)


def maybe_apply_allocation_sell(
    coord: RuntimeCoordinator,
    app: AppConfig,
    *,
    strategy: object,
    ap: ApprovedIntent,
    match_evidence: dict[str, Any],
    correlation_id: str,
    intent_extensions: dict[str, Any] | None,
    run_id: str,
    apply_local_shadow_fill: bool = False,
) -> None:
    if not should_apply_allocation_sell_on_submit(
        match_evidence, apply_local_shadow_fill=apply_local_shadow_fill
    ):
        return
    ledger = coord.allocation_ledger
    if ledger is None:
        return
    intent = ap.intent
    if not isinstance(intent, (ExitIntent, ReduceIntent)) or intent.side != Side.SELL:
        return
    owner_id = resolve_owner_id(strategy, intent, intent_extensions=intent_extensions)
    qty = fill_qty_for_allocation(intent, match_evidence, intent.size)
    mut = ledger.apply_sell(
        owner_id,
        intent.token_id,
        qty,
        correlation_id=correlation_id,
        reservation_id=str(ap.client_order_id),
    )
    mut.source = "immediate_match"
    _emit_allocation_fact(coord, run_id, mut, correlation_id=correlation_id)


def maybe_note_allocation_exit_order_live(
    coord: RuntimeCoordinator,
    app: AppConfig,
    *,
    strategy: object,
    ap: ApprovedIntent,
    match_evidence: dict[str, Any],
    correlation_id: str,
    intent_extensions: dict[str, Any] | None,
    run_id: str,
) -> None:
    """Record that a SELL ack is resting on the book; reservation stays active.

    TODO(P4+): promote to allocation_sell_applied on WS-confirmed fill or REST
    order status transition to matched.
    """
    ledger = coord.allocation_ledger
    if ledger is None:
        return
    intent = ap.intent
    if not isinstance(intent, (ExitIntent, ReduceIntent)) or intent.side != Side.SELL:
        return
    owner_id = resolve_owner_id(strategy, intent, intent_extensions=intent_extensions)
    tid = str(intent.token_id)
    payload: dict[str, Any] = {
        "event": "allocation_exit_order_live",
        "owner_id": owner_id,
        "token_id": tid,
        "order_qty": str(intent.size),
        "reservation_id": str(ap.client_order_id),
        "match_status": str(match_evidence.get("match_status", "")),
        **_ledger_balance_fields(ledger, owner_id, tid),
    }
    if coord.allocation_ledger_sink is None or coord.allocation_ledger_run_id is None:
        return
    coord.allocation_ledger_sink.write(
        make_fact(
            FACT_TYPE_ALLOCATION_LEDGER,
            coord.allocation_ledger_run_id,
            payload,
            correlation_id=correlation_id,
        )
    )


def maybe_reserve_exit_allocation(
    coord: RuntimeCoordinator,
    app: AppConfig,
    *,
    strategy: object,
    ap: ApprovedIntent,
    correlation_id: str,
    intent_extensions: dict[str, Any] | None,
    run_id: str,
) -> None:
    ledger = coord.allocation_ledger
    if ledger is None:
        return
    intent = ap.intent
    if not isinstance(intent, (ExitIntent, ReduceIntent)) or intent.side != Side.SELL:
        return
    owner_id = resolve_owner_id(strategy, intent, intent_extensions=intent_extensions)
    mut = ledger.reserve_exit(
        owner_id,
        intent.token_id,
        intent.size,
        str(ap.client_order_id),
    )
    _emit_allocation_fact(coord, run_id, mut, correlation_id=correlation_id)


def maybe_release_exit_reservation(
    coord: RuntimeCoordinator,
    app: AppConfig,
    *,
    client_order_id: str,
    correlation_id: str,
    run_id: str,
) -> None:
    ledger = coord.allocation_ledger
    if ledger is None:
        return
    mut = ledger.release_reservation(client_order_id, reason="reject", source="oms_reject")
    if mut is not None:
        _emit_allocation_fact(coord, run_id, mut, correlation_id=correlation_id)


def maybe_clamp_allocations_to_venue(
    coord: RuntimeCoordinator,
    *,
    run_id: str,
) -> None:
    ledger = coord.allocation_ledger
    if ledger is None:
        return
    clamps = ledger.clamp_to_venue_positions(coord.wallet.positions)
    for row in clamps:
        _emit_allocation_fact(coord, run_id, row, event="allocation_clamped")
