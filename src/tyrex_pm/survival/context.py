"""Build SurvivorLegContext from paired-binary runtime state (M1)."""

from __future__ import annotations

import time
from decimal import Decimal

from tyrex_pm.runtime.config import PairedBinaryStrategyConfig, SurvivalConfig
from tyrex_pm.strategies.paired_binary.state import PairedBinaryRuntimeState
from tyrex_pm.survival.models import SurvivorLegContext, SurvivorLegState, SurvivorTargetMode, SurvivorTargetPlan
from tyrex_pm.survival.target_policy import DYNAMIC_DOWNGRADE_CHAIN, SurvivorTargetPolicy, TargetSelectionResult


def _dec(value: Decimal | None, fallback: Decimal) -> Decimal:
    return value if value is not None else fallback


def build_survivor_leg_context(
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
    survival: SurvivalConfig,
    *,
    loser_leg: str,
    loser_exit_fill: Decimal,
    seconds_to_close: float | None = None,
) -> SurvivorLegContext | None:
    survivor_leg: str = "no" if loser_leg == "yes" else "yes"
    loser_rt = state.yes if loser_leg == "yes" else state.no

    survivor_qty = state.effective_qty
    if survivor_qty <= 0:
        return None

    ledger_source = "matched_cash"
    yes_entry_cash = state.yes.entry_cash
    no_entry_cash = state.no.entry_cash
    yes_entry_qty = state.yes.entry_qty
    no_entry_qty = state.no.entry_qty

    if yes_entry_cash is None or no_entry_cash is None:
        ledger_source = "price_qty_fallback"
        yes_px = state.yes_entry or Decimal("0")
        no_px = state.no_entry or Decimal("0")
        eq = yes_entry_qty or survivor_qty
        nq = no_entry_qty or survivor_qty
        yes_entry_cash = yes_px * eq
        no_entry_cash = no_px * nq
        yes_entry_qty = eq
        no_entry_qty = nq

    loser_exit_qty = loser_rt.exit_qty
    loser_exit_cash = loser_rt.exit_cash
    if loser_exit_cash is None or loser_exit_qty is None or loser_exit_qty <= 0:
        if ledger_source == "matched_cash":
            ledger_source = "fill_price_fallback"
        fill_qty = loser_rt.exit_qty or survivor_qty
        loser_exit_qty = fill_qty
        loser_exit_cash = loser_exit_fill * fill_qty

    slippage = survival.target_policy.slippage_buffer
    if slippage is None:
        slippage = cfg.slippage_buffer

    total_notional = (yes_entry_cash or Decimal("0")) + (no_entry_cash or Decimal("0")) + (
        loser_exit_cash or Decimal("0")
    )
    fee_bps = survival.target_policy.estimated_fee_bps
    estimated_fees = total_notional * fee_bps / Decimal("10000")

    desired_total = Decimal("0")
    if state.desired_net_profit_per_pair is not None:
        desired_total = state.desired_net_profit_per_pair * survivor_qty

    return SurvivorLegContext(
        survivor_leg=survivor_leg,  # type: ignore[arg-type]
        yes_entry_qty=_dec(yes_entry_qty, survivor_qty),
        no_entry_qty=_dec(no_entry_qty, survivor_qty),
        yes_entry_cash=_dec(yes_entry_cash, Decimal("0")),
        no_entry_cash=_dec(no_entry_cash, Decimal("0")),
        loser_exit_qty=_dec(loser_exit_qty, survivor_qty),
        loser_exit_cash=_dec(loser_exit_cash, Decimal("0")),
        survivor_remaining_qty=survivor_qty,
        estimated_fees=estimated_fees,
        slippage_buffer=slippage,
        desired_net_profit_total=desired_total,
        seconds_to_close=seconds_to_close,
        loser_exit_ts=time.time(),
        ledger_source=ledger_source,
    )


def apply_survival_target_plan(
    state: PairedBinaryRuntimeState,
    *,
    survivor_leg: str,
    plan,
    survivor_bid: Decimal | None = None,
    seconds_to_close: float | None = None,
) -> None:
    if survivor_leg == "yes":
        state.yes_planned_target = plan.required_survivor_exit_price
        state.yes_target = plan.trigger_target
    else:
        state.no_planned_target = plan.required_survivor_exit_price
        state.no_target = plan.trigger_target

    state.survivor_leg_state = {
        "survivor_bid_0": str(survivor_bid) if survivor_bid is not None else None,
        "selected_target": str(plan.trigger_target),
        "selected_mode": plan.mode.value,
        "loser_exit_ts": plan.evidence.get("loser_exit_ts", str(time.time())),
        "seconds_to_close_0": seconds_to_close,
        "available_survival_time": seconds_to_close,
    }


def select_and_apply_survival_target(
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
    survival: SurvivalConfig,
    *,
    loser_leg: str,
    loser_exit_fill: Decimal,
    seconds_to_close: float | None = None,
    survivor_bid: Decimal | None = None,
) -> TargetSelectionResult | None:
    ctx = build_survivor_leg_context(
        state,
        cfg,
        survival,
        loser_leg=loser_leg,
        loser_exit_fill=loser_exit_fill,
        seconds_to_close=seconds_to_close,
    )
    if ctx is None:
        return None
    policy = SurvivorTargetPolicy(survival.target_policy)
    result = policy.select_plan(ctx)
    apply_survival_target_plan(
        state,
        survivor_leg=ctx.survivor_leg,
        plan=result.plan,
        survivor_bid=survivor_bid,
        seconds_to_close=seconds_to_close,
    )
    return result


def survivor_leg_state_from_dict(raw: dict | None) -> SurvivorLegState | None:
    if not raw:
        return None
    mode_raw = raw.get("selected_mode", SurvivorTargetMode.FULL_RECOVERY.value)
    try:
        mode = SurvivorTargetMode(str(mode_raw))
    except ValueError:
        mode = SurvivorTargetMode.FULL_RECOVERY
    bid_raw = raw.get("survivor_bid_0")
    return SurvivorLegState(
        survivor_bid_0=Decimal(str(bid_raw)) if bid_raw else None,
        selected_target=Decimal(str(raw.get("selected_target", "0"))),
        selected_mode=mode,
        loser_exit_ts=float(raw.get("loser_exit_ts", 0)),
        seconds_to_close_0=raw.get("seconds_to_close_0"),
        available_survival_time=raw.get("available_survival_time"),
    )


def downgrade_survivor_target_one_step(
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
    survival: SurvivalConfig,
    *,
    survivor_leg: str,
    loser_leg: str,
    loser_exit_fill: Decimal,
    seconds_to_close: float | None = None,
    survivor_bid: Decimal | None = None,
) -> tuple[SurvivorTargetMode, SurvivorTargetMode, SurvivorTargetPlan] | None:
    """Move survivor target one step down the dynamic downgrade chain."""
    raw = state.survivor_leg_state or {}
    try:
        current = SurvivorTargetMode(str(raw.get("selected_mode", SurvivorTargetMode.FULL_RECOVERY.value)))
    except ValueError:
        current = SurvivorTargetMode.FULL_RECOVERY
    try:
        idx = DYNAMIC_DOWNGRADE_CHAIN.index(current)
    except ValueError:
        return None
    if idx + 1 >= len(DYNAMIC_DOWNGRADE_CHAIN):
        return None
    next_mode = DYNAMIC_DOWNGRADE_CHAIN[idx + 1]
    ctx = build_survivor_leg_context(
        state,
        cfg,
        survival,
        loser_leg=loser_leg,
        loser_exit_fill=loser_exit_fill,
        seconds_to_close=seconds_to_close,
    )
    if ctx is None:
        return None
    policy = SurvivorTargetPolicy(survival.target_policy)
    plan = policy.plan_for_mode(ctx, next_mode)
    trailing = raw.get("trailing_stop")
    apply_survival_target_plan(
        state,
        survivor_leg=survivor_leg,
        plan=plan,
        survivor_bid=survivor_bid,
        seconds_to_close=seconds_to_close,
    )
    if trailing is not None and state.survivor_leg_state is not None:
        state.survivor_leg_state["trailing_stop"] = trailing
    return current, next_mode, plan
