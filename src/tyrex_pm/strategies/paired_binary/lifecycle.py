"""Paired binary lifecycle transitions (Phase 4.6)."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.time import monotonic_s
from tyrex_pm.strategies.paired_binary.entry_eval import LegBook
from tyrex_pm.strategies.paired_binary.state import (
    LegRuntime,
    PairedBinaryPhase,
    PairedBinaryRuntimeState,
)


def resolve_min_effective_pair_qty(
    configured: Decimal | None,
    *,
    venue_min_size: Decimal,
) -> Decimal:
    if configured is not None and configured > 0:
        return configured
    return venue_min_size


def compute_effective_qty(yes_qty: Decimal, no_qty: Decimal) -> Decimal:
    return min(yes_qty, no_qty)


def transition_phase(
    state: PairedBinaryRuntimeState,
    new_phase: PairedBinaryPhase,
    *,
    reason: str | None = None,
) -> tuple[PairedBinaryPhase, PairedBinaryPhase, str | None]:
    old = state.phase
    state.phase = new_phase
    return old, new_phase, reason


def mark_both_legs_filled(
    state: PairedBinaryRuntimeState,
    *,
    yes_qty: Decimal,
    no_qty: Decimal,
    yes_entry: Decimal,
    no_entry: Decimal,
    entry_price_source: str,
    yes_entry_price_source: str | None = None,
    no_entry_price_source: str | None = None,
) -> Decimal | None:
    """Both legs filled; wait for sellable inventory before arming monitor."""
    effective = compute_effective_qty(yes_qty, no_qty)
    state.effective_qty = effective
    state.yes.allocation_final_qty = yes_qty
    state.no.allocation_final_qty = no_qty
    state.yes_entry = yes_entry
    state.no_entry = no_entry
    state.yes.entry_vwap = yes_entry
    state.no.entry_vwap = no_entry
    state.entry_price_source = entry_price_source
    state.yes_entry_price_source = yes_entry_price_source or entry_price_source
    state.no_entry_price_source = no_entry_price_source or entry_price_source
    state.phase = PairedBinaryPhase.BOTH_LEGS_FILLED
    if yes_qty > no_qty:
        return yes_qty - no_qty
    if no_qty > yes_qty:
        return no_qty - yes_qty
    return None


def activate_monitoring(
    state: PairedBinaryRuntimeState,
    *,
    yes_book: LegBook,
    no_book: LegBook,
) -> None:
    """Arm dual stop-loss monitor once both legs are sellable."""
    now = monotonic_s()
    state.phase = PairedBinaryPhase.BOTH_LEGS_ACTIVE
    state.activation_ts = now
    state.pair_opened_ts = now
    state.yes_activation_bid = yes_book.bid
    state.no_activation_bid = no_book.bid


def activate_both_legs(
    state: PairedBinaryRuntimeState,
    *,
    yes_qty: Decimal,
    no_qty: Decimal,
    yes_entry: Decimal,
    no_entry: Decimal,
    entry_price_source: str,
    yes_book: LegBook | None = None,
    no_book: LegBook | None = None,
) -> Decimal | None:
    """Legacy helper: fill + immediate activation when books provided."""
    excess = mark_both_legs_filled(
        state,
        yes_qty=yes_qty,
        no_qty=no_qty,
        yes_entry=yes_entry,
        no_entry=no_entry,
        entry_price_source=entry_price_source,
    )
    if yes_book is not None and no_book is not None:
        activate_monitoring(state, yes_book=yes_book, no_book=no_book)
    else:
        state.phase = PairedBinaryPhase.BOTH_LEGS_ACTIVE
        state.pair_opened_ts = monotonic_s()
        state.activation_ts = state.pair_opened_ts
    return excess


def apply_yes_stop_loss(state: PairedBinaryRuntimeState, cfg: PairedBinaryStrategyConfig) -> None:
    """YES stop-loss fired: survivor is NO (legacy — prefer prepare_yes_stop_trigger)."""
    from tyrex_pm.strategies.paired_binary.exit_engine import prepare_yes_stop_trigger

    prepare_yes_stop_trigger(state, cfg)


def apply_no_stop_loss(state: PairedBinaryRuntimeState, cfg: PairedBinaryStrategyConfig) -> None:
    """NO stop-loss fired: survivor is YES (legacy — prefer prepare_no_stop_trigger)."""
    from tyrex_pm.strategies.paired_binary.exit_engine import prepare_no_stop_trigger

    prepare_no_stop_trigger(state, cfg)


def choose_dual_stop_leg(
    *,
    yes_loss: Decimal,
    no_loss: Decimal,
    yes_spread: Decimal,
    no_spread: Decimal,
) -> str:
    """Deterministic tie-break: larger loss → wider spread → NO first."""
    if yes_loss > no_loss:
        return "yes"
    if no_loss > yes_loss:
        return "no"
    if yes_spread > no_spread:
        return "yes"
    if no_spread > yes_spread:
        return "no"
    return "no"


def leg_for_token(state: PairedBinaryRuntimeState, token_id: str) -> LegRuntime | None:
    if token_id == state.yes_token_id:
        return state.yes
    if token_id == state.no_token_id:
        return state.no
    return None


def leg_name(state: PairedBinaryRuntimeState, token_id: str) -> str | None:
    if token_id == state.yes_token_id:
        return "yes"
    if token_id == state.no_token_id:
        return "no"
    return None
