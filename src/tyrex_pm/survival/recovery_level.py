"""Pair breakeven / recovery level for simplified Phase 1 survival."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.runtime.config import PairedBinaryStrategyConfig, RecoveryLevelConfig, SurvivalConfig
from tyrex_pm.strategies.paired_binary.state import PairedBinaryRuntimeState


@dataclass(frozen=True)
class RecoveryLevelResult:
    survivor_leg: str
    total_entry_cash: Decimal
    loser_exit_cash: Decimal
    survivor_qty: Decimal
    breakeven_price: Decimal
    activation_price: Decimal
    desired_buffer: Decimal
    slippage_buffer: Decimal


def _ledger_cash(
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
) -> tuple[Decimal, Decimal, Decimal, str]:
    survivor_qty = state.effective_qty
    yes_cash = state.yes.entry_cash
    no_cash = state.no.entry_cash
    source = "matched_cash"
    if yes_cash is None or no_cash is None:
        source = "price_qty_fallback"
        yes_cash = (state.yes_entry or Decimal("0")) * survivor_qty
        no_cash = (state.no_entry or Decimal("0")) * survivor_qty
    total_entry = yes_cash + no_cash
    loser_cash = Decimal("0")
    if state.yes.exit_cash is not None and state.yes.triggered:
        loser_cash = state.yes.exit_cash
    elif state.no.exit_cash is not None and state.no.triggered:
        loser_cash = state.no.exit_cash
    return total_entry, loser_cash, survivor_qty, source


def compute_recovery_level(
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
    survival: SurvivalConfig,
    *,
    survivor_leg: str,
    loser_exit_fill: Decimal | None = None,
) -> RecoveryLevelResult | None:
    """Breakeven price: (total_entry - loser_exit + buffers) / survivor_qty."""
    rcfg = survival.recovery_level
    qty = state.effective_qty
    if qty <= 0:
        return None

    total_entry, loser_cash, survivor_qty, _ = _ledger_cash(state, cfg)
    if loser_cash <= 0 and loser_exit_fill is not None:
        loser_rt = state.yes if survivor_leg == "no" else state.no
        fill_qty = loser_rt.exit_qty or survivor_qty
        loser_cash = loser_exit_fill * fill_qty

    slippage = rcfg.slippage_buffer
    if slippage is None:
        slippage = cfg.slippage_buffer
    desired_buffer = rcfg.desired_buffer
    numerator = total_entry - loser_cash + desired_buffer + slippage
    breakeven = numerator / survivor_qty
    breakeven = min(Decimal("0.99"), max(Decimal("0.01"), breakeven))
    activation = breakeven + rcfg.activation_buffer

    return RecoveryLevelResult(
        survivor_leg=survivor_leg,
        total_entry_cash=total_entry,
        loser_exit_cash=loser_cash,
        survivor_qty=survivor_qty,
        breakeven_price=breakeven,
        activation_price=activation,
        desired_buffer=desired_buffer,
        slippage_buffer=slippage,
    )


def apply_recovery_level(
    state: PairedBinaryRuntimeState,
    result: RecoveryLevelResult,
    *,
    survivor_bid: Decimal | None = None,
    seconds_to_close: float | None = None,
    loser_exit_ts: float | None = None,
) -> None:
    """Persist recovery level; set survivor target reference to breakeven (not full recovery)."""
    import time

    leg = result.survivor_leg
    if leg == "yes":
        state.yes_planned_target = result.breakeven_price
        state.yes_target = result.breakeven_price
    else:
        state.no_planned_target = result.breakeven_price
        state.no_target = result.breakeven_price

    raw = state.survivor_leg_state or {}
    state.survivor_leg_state = {
        **raw,
        "survivor_bid_0": str(survivor_bid) if survivor_bid is not None else raw.get("survivor_bid_0"),
        "breakeven_price": str(result.breakeven_price),
        "activation_price": str(result.activation_price),
        "total_entry_cash": str(result.total_entry_cash),
        "loser_exit_cash": str(result.loser_exit_cash),
        "survivor_qty": str(result.survivor_qty),
        "loser_exit_ts": loser_exit_ts if loser_exit_ts is not None else raw.get("loser_exit_ts", time.time()),
        "seconds_to_close_0": seconds_to_close,
        "available_survival_time": seconds_to_close,
    }


def breakeven_from_state(state: PairedBinaryRuntimeState) -> Decimal | None:
    raw = state.survivor_leg_state or {}
    bp = raw.get("breakeven_price")
    if bp is None:
        return None
    return Decimal(str(bp))


def setup_simplified_survivor_after_loser_exit(
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
    survival: SurvivalConfig,
    *,
    loser_leg: str,
    loser_exit_fill: Decimal,
    survivor_bid: Decimal | None = None,
    seconds_to_close: float | None = None,
) -> tuple[RecoveryLevelResult, Decimal] | None:
    """Apply recovery level + hard floor immediately after loser exit (Phase 1 simplified)."""
    import time

    from tyrex_pm.survival.survivor_floor import SurvivorHardFloor

    survivor_leg: str = "no" if loser_leg == "yes" else "yes"
    recovery = compute_recovery_level(
        state,
        cfg,
        survival,
        survivor_leg=survivor_leg,
        loser_exit_fill=loser_exit_fill,
    )
    if recovery is None:
        return None

    apply_recovery_level(
        state,
        recovery,
        survivor_bid=survivor_bid,
        seconds_to_close=seconds_to_close,
        loser_exit_ts=time.time(),
    )

    survivor_entry = state.yes_entry if survivor_leg == "yes" else state.no_entry
    if survivor_entry is None:
        return None
    floor_price = SurvivorHardFloor().compute_floor_price(
        survivor_entry=survivor_entry,
        cfg=survival.survivor_floor,
    )
    if state.survivor_leg_state is None:
        state.survivor_leg_state = {}
    state.survivor_leg_state["hard_floor_price"] = str(floor_price)
    state.survivor_leg_state["survivor_leg"] = survivor_leg
    return recovery, floor_price
