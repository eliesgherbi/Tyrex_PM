"""Detection and reporting for market-resolution survivor exits (no OMS sell)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from tyrex_pm.core.ids import TokenId
from tyrex_pm.strategies.paired_binary.state import LegRuntime, PairedBinaryPhase

if TYPE_CHECKING:
    from tyrex_pm.runtime.config import PairedBinaryStrategyConfig
    from tyrex_pm.strategies.paired_binary.state import PairedBinaryRuntimeState


@dataclass(frozen=True)
class SurvivorResolutionContext:
    survivor_leg: str
    survivor_leg_runtime: LegRuntime
    state_before: str
    survivor_qty_before_clamp: Decimal
    survivor_qty_after_clamp: Decimal
    venue_qty: Decimal
    resolution_detected_reason: str


def detect_survivor_resolution_exit(
    state: PairedBinaryRuntimeState,
    *,
    phase_before: PairedBinaryPhase,
    yes_qty: Decimal,
    no_qty: Decimal,
    venue_yes_qty: Decimal,
    venue_no_qty: Decimal,
) -> SurvivorResolutionContext | None:
    """Return context when survivor leg flat without OMS exit cashflow."""
    leg_info: tuple[str, LegRuntime, Decimal, Decimal] | None = None

    if phase_before == PairedBinaryPhase.ONLY_NO_ACTIVE:
        if state.no.exit_cash is None and not state.no.exit_submitted:
            leg_info = ("no", state.no, state.effective_qty, no_qty)
    elif phase_before == PairedBinaryPhase.ONLY_YES_ACTIVE:
        if state.yes.exit_cash is None and not state.yes.exit_submitted:
            leg_info = ("yes", state.yes, state.effective_qty, yes_qty)
    elif phase_before in {
        PairedBinaryPhase.STOP_PENDING_NO,
        PairedBinaryPhase.TP_PENDING_NO,
        PairedBinaryPhase.EXITING_NO,
    }:
        if state.no.exit_cash is None and not state.no.exit_submitted and no_qty <= 0:
            leg_info = ("no", state.no, state.effective_qty, no_qty)
    elif phase_before in {
        PairedBinaryPhase.STOP_PENDING_YES,
        PairedBinaryPhase.TP_PENDING_YES,
        PairedBinaryPhase.EXITING_YES,
    }:
        if state.yes.exit_cash is None and not state.yes.exit_submitted and yes_qty <= 0:
            leg_info = ("yes", state.yes, state.effective_qty, yes_qty)

    if leg_info is None:
        if (
            state.no.exit_cash is None
            and not state.no.exit_submitted
            and state.yes.exit_cash is not None
            and no_qty <= 0
        ):
            leg_info = ("no", state.no, state.effective_qty, no_qty)
        elif (
            state.yes.exit_cash is None
            and not state.yes.exit_submitted
            and state.no.exit_cash is not None
            and yes_qty <= 0
        ):
            leg_info = ("yes", state.yes, state.effective_qty, yes_qty)

    if leg_info is None:
        return None

    leg_name, leg_rt, qty_before, qty_after = leg_info
    if qty_before <= 0 and qty_after <= 0:
        return None

    venue_qty = venue_no_qty if leg_name == "no" else venue_yes_qty
    reason = "allocation_clamped_to_zero_without_oms_exit"
    if venue_qty <= 0:
        reason = "venue_position_zero_without_oms_exit"

    return SurvivorResolutionContext(
        survivor_leg=leg_name,
        survivor_leg_runtime=leg_rt,
        state_before=phase_before.value,
        survivor_qty_before_clamp=qty_before,
        survivor_qty_after_clamp=qty_after,
        venue_qty=venue_qty,
        resolution_detected_reason=reason,
    )


def venue_position_qty(coord, owner_id: str, token_id: str) -> Decimal:
    wallet = getattr(coord, "wallet", None)
    if wallet is None:
        return Decimal("0")
    positions = getattr(wallet, "positions", {}) or {}
    pos = positions.get(TokenId(token_id))
    if pos is None:
        return Decimal("0")
    raw = getattr(pos, "size", None) or getattr(pos, "qty", None)
    if raw is None:
        return Decimal("0")
    try:
        return Decimal(str(raw))
    except Exception:
        return Decimal("0")


def resolution_accounting_payload(
    ctx: SurvivorResolutionContext,
    *,
    cfg: PairedBinaryStrategyConfig,
    state_after: str,
) -> dict[str, Any]:
    return {
        "survivor_leg": ctx.survivor_leg,
        "survivor_qty_before_clamp": str(ctx.survivor_qty_before_clamp),
        "survivor_qty_after_clamp": str(ctx.survivor_qty_after_clamp),
        "venue_qty": str(ctx.venue_qty),
        "state_before": ctx.state_before,
        "state_after": state_after,
        "market_id": cfg.market_id,
        "condition_id": cfg.condition_id,
        "event_end_ts": cfg.event_end_ts,
        "resolution_detected_reason": ctx.resolution_detected_reason,
        "known_exit_cashflow": False,
        "pnl_status": "resolution_cashflow_missing",
        "manual_reconciliation_required": True,
        "terminal_reason": "market_resolution_without_oms_exit",
    }
