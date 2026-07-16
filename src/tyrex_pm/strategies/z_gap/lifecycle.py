"""Z-Gap fill reconciliation, activation, and allocation checks (A0.7)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.state.entry_fill_lifecycle import EntryFillStatus, OrderFillSnapshot, resolve_leg_fill_snapshot
from tyrex_pm.strategies.z_gap.state import InvalidZGapTransition, ZGapLifecycleState, ZGapPhase


@dataclass(frozen=True)
class EntryFillOutcome:
    event: str
    activated: bool
    failure: bool
    failure_reason: str | None = None


@dataclass(frozen=True)
class ExitFillOutcome:
    event: str
    closed: bool
    residual: Decimal
    failure: bool
    failure_reason: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def allocated_quantity(coord: RuntimeCoordinator, *, owner_id: str, token_id: str) -> Decimal:
    ledger = coord.allocation_ledger
    if ledger is None:
        return Decimal("0")
    return ledger.get_allocated(owner_id, TokenId(token_id))


def verify_allocation_reconciled(
    coord: RuntimeCoordinator,
    lifecycle: ZGapLifecycleState,
) -> tuple[bool, Decimal, Decimal]:
    if lifecycle.token_id is None:
        return lifecycle.active_quantity == 0, lifecycle.active_quantity, Decimal("0")
    alloc = allocated_quantity(coord, owner_id=lifecycle.owner_id, token_id=lifecycle.token_id)
    return alloc == lifecycle.active_quantity, lifecycle.active_quantity, alloc


def mark_entry_submitted(
    lifecycle: ZGapLifecycleState,
    *,
    order_id: str,
    requested_shares: Decimal,
    selected_leg: str,
    token_id: str,
    model_p: Decimal | None,
    entry_z: Decimal | None,
    entry_edge: Decimal | None,
    now: datetime | None = None,
) -> None:
    if not lifecycle.can_submit_entry():
        raise InvalidZGapTransition(
            from_phase=lifecycle.phase,
            to_phase=ZGapPhase.ENTRY_PENDING,
            reason="entry_not_allowed",
        )
    now = now or _utc_now()
    lifecycle.entry_attempted = True
    lifecycle.entry_submitted = True
    lifecycle.selected_leg = selected_leg
    lifecycle.token_id = token_id
    lifecycle.entry_order_id = order_id
    lifecycle.entry_requested_shares = requested_shares
    lifecycle.entry_model_p = model_p
    lifecycle.entry_z = entry_z
    lifecycle.entry_edge = entry_edge
    lifecycle.entry_ts = now
    lifecycle.transition(ZGapPhase.ENTRY_PENDING, now=now)


def reconcile_entry_fill(
    lifecycle: ZGapLifecycleState,
    snapshot: OrderFillSnapshot,
    *,
    coord: RuntimeCoordinator | None = None,
    avg_price: Decimal | None = None,
    fee: Decimal | None = None,
    now: datetime | None = None,
) -> EntryFillOutcome:
    if lifecycle.phase != ZGapPhase.ENTRY_PENDING:
        return EntryFillOutcome(event="ignored", activated=False, failure=False)

    now = now or _utc_now()
    filled = snapshot.filled_qty
    if filled < 0:
        lifecycle.transition(ZGapPhase.FAILED, reason="negative_fill_qty", now=now)
        return EntryFillOutcome(event="entry_failed", activated=False, failure=True, failure_reason="negative_fill_qty")

    if snapshot.status == EntryFillStatus.FAILED:
        lifecycle.transition(ZGapPhase.FAILED, reason="entry_order_failed", now=now)
        return EntryFillOutcome(
            event="entry_failed",
            activated=False,
            failure=True,
            failure_reason="entry_order_failed",
        )

    if filled == 0:
        if snapshot.status in {EntryFillStatus.SUBMITTED, EntryFillStatus.RESTING}:
            return EntryFillOutcome(event="entry_pending", activated=False, failure=False)
        lifecycle.transition(ZGapPhase.DONE, now=now)
        lifecycle.exited_this_window = True
        return EntryFillOutcome(event="entry_unfilled", activated=False, failure=False)

    if coord is not None and lifecycle.token_id is not None:
        alloc = allocated_quantity(coord, owner_id=lifecycle.owner_id, token_id=lifecycle.token_id)
        if alloc > 0 and alloc != filled:
            lifecycle.transition(ZGapPhase.FAILED, reason="allocation_entry_mismatch", now=now)
            return EntryFillOutcome(
                event="entry_failed",
                activated=False,
                failure=True,
                failure_reason="allocation_entry_mismatch",
            )

    lifecycle.entry_filled_shares = filled
    lifecycle.active_quantity = filled
    lifecycle.entry_avg_price = avg_price
    lifecycle.entry_fee = fee
    lifecycle.position_activated = True
    lifecycle.transition(ZGapPhase.ACTIVE, now=now)
    return EntryFillOutcome(event="entry_filled", activated=True, failure=False)


def mark_exit_triggered(
    lifecycle: ZGapLifecycleState,
    *,
    exit_reason: str,
    now: datetime | None = None,
) -> None:
    if lifecycle.phase != ZGapPhase.ACTIVE:
        raise InvalidZGapTransition(
            from_phase=lifecycle.phase,
            to_phase=ZGapPhase.EXIT_PENDING,
            reason="not_active",
        )
    now = now or _utc_now()
    lifecycle.exit_triggered = True
    lifecycle.exit_reason = exit_reason
    lifecycle.exit_trigger_ts = now
    lifecycle.transition(ZGapPhase.EXIT_PENDING, now=now)


def mark_exit_submitted(
    lifecycle: ZGapLifecycleState,
    *,
    order_id: str,
    requested_shares: Decimal,
) -> None:
    lifecycle.exit_order_id = order_id
    lifecycle.exit_requested_shares = requested_shares
    lifecycle.exit_attempts += 1


def reconcile_exit_fill(
    lifecycle: ZGapLifecycleState,
    *,
    filled_qty: Decimal,
    avg_price: Decimal | None = None,
    coord: RuntimeCoordinator | None = None,
    now: datetime | None = None,
) -> ExitFillOutcome:
    if lifecycle.phase != ZGapPhase.EXIT_PENDING:
        return ExitFillOutcome(event="ignored", closed=False, residual=lifecycle.active_quantity, failure=False)

    now = now or _utc_now()
    if filled_qty < 0:
        lifecycle.transition(ZGapPhase.FAILED, reason="negative_exit_fill", now=now)
        return ExitFillOutcome(
            event="exit_failed",
            closed=False,
            residual=lifecycle.active_quantity,
            failure=True,
            failure_reason="negative_exit_fill",
        )

    if filled_qty == 0:
        if lifecycle.phase == ZGapPhase.EXIT_PENDING:
            lifecycle.transition(ZGapPhase.ACTIVE, now=now)
            lifecycle.exit_order_id = None
            lifecycle.exit_triggered = False
        return ExitFillOutcome(event="exit_unfilled", closed=False, residual=lifecycle.active_quantity, failure=False)

    if filled_qty > lifecycle.active_quantity:
        lifecycle.transition(ZGapPhase.FAILED, reason="oversell", now=now)
        return ExitFillOutcome(
            event="exit_failed",
            closed=False,
            residual=lifecycle.active_quantity,
            failure=True,
            failure_reason="oversell",
        )

    lifecycle.exit_filled_shares += filled_qty
    lifecycle.active_quantity -= filled_qty
    if avg_price is not None:
        lifecycle.exit_avg_price = avg_price

    if coord is not None and lifecycle.token_id is not None:
        ok, active, alloc = verify_allocation_reconciled(coord, lifecycle)
        if active > 0 and alloc < active:
            lifecycle.transition(ZGapPhase.FAILED, reason="allocation_exit_mismatch", now=now)
            return ExitFillOutcome(
                event="exit_failed",
                closed=False,
                residual=lifecycle.active_quantity,
                failure=True,
                failure_reason="allocation_exit_mismatch",
            )
        if active == 0 and alloc > 0:
            lifecycle.transition(ZGapPhase.FAILED, reason="allocation_exit_mismatch", now=now)
            return ExitFillOutcome(
                event="exit_failed",
                closed=False,
                residual=lifecycle.active_quantity,
                failure=True,
                failure_reason="allocation_exit_mismatch",
            )

    if lifecycle.active_quantity > 0:
        lifecycle.transition(ZGapPhase.ACTIVE, now=now)
        lifecycle.exit_order_id = None
        lifecycle.exit_triggered = False
        return ExitFillOutcome(
            event="exit_partial",
            closed=False,
            residual=lifecycle.active_quantity,
            failure=False,
        )

    lifecycle.position_closed = True
    lifecycle.exited_this_window = True
    lifecycle.exit_completed_ts = now
    lifecycle.transition(ZGapPhase.DONE, now=now)
    return ExitFillOutcome(event="exit_closed", closed=True, residual=Decimal("0"), failure=False)


def resolve_entry_snapshot(
    coord: RuntimeCoordinator,
    lifecycle: ZGapLifecycleState,
) -> OrderFillSnapshot | None:
    if lifecycle.token_id is None:
        return None
    return resolve_leg_fill_snapshot(
        coord,
        token_id=TokenId(lifecycle.token_id),
        owner_id=lifecycle.owner_id,
        client_order_id=lifecycle.entry_order_id,
        submitted_qty=lifecycle.entry_requested_shares,
    )
