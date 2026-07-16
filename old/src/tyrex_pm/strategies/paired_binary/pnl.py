"""Pair-level percentage PnL math for paired binary (Phase 4.6)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.runtime.config import PairedBinaryStrategyConfig
from tyrex_pm.strategies.paired_binary.state import PairedBinaryRuntimeState


@dataclass(frozen=True)
class PairPnLBudgets:
    pair_cost: Decimal
    loss_budget: Decimal
    profit_budget: Decimal
    desired_net_profit_per_pair: Decimal
    yes_planned_stop: Decimal
    yes_trigger_stop: Decimal
    no_planned_stop: Decimal
    no_trigger_stop: Decimal


@dataclass(frozen=True)
class SurvivorTargetPlan:
    planned_target: Decimal
    trigger_target: Decimal
    realized_loser_loss: Decimal | None = None
    required_winner_gain: Decimal | None = None


def pair_cost_from_entries(yes_entry: Decimal, no_entry: Decimal) -> Decimal:
    return yes_entry + no_entry


def loss_budget(pair_cost: Decimal, pair_stop_loss_pct: Decimal) -> Decimal:
    return pair_cost * pair_stop_loss_pct


def profit_budget(pair_cost: Decimal, pair_take_profit_pct: Decimal) -> Decimal:
    return pair_cost * pair_take_profit_pct


def desired_net_profit_per_pair(
    pair_cost: Decimal,
    *,
    pair_stop_loss_pct: Decimal,
    pair_take_profit_pct: Decimal,
) -> Decimal:
    return pair_cost * (pair_take_profit_pct - pair_stop_loss_pct)


def planned_stop_price(loser_entry: Decimal, loss_budget_value: Decimal) -> Decimal:
    return loser_entry - loss_budget_value


def trigger_stop_price(planned_stop: Decimal, slippage_buffer: Decimal) -> Decimal:
    return planned_stop + slippage_buffer


def planned_target_price(survivor_entry: Decimal, profit_budget_value: Decimal) -> Decimal:
    return survivor_entry + profit_budget_value


def trigger_target_price(planned_target: Decimal, slippage_buffer: Decimal) -> Decimal:
    return planned_target + slippage_buffer


def compute_pair_pnl_budgets(
    yes_entry: Decimal,
    no_entry: Decimal,
    *,
    pair_stop_loss_pct: Decimal,
    pair_take_profit_pct: Decimal,
    slippage_buffer: Decimal,
) -> PairPnLBudgets:
    pc = pair_cost_from_entries(yes_entry, no_entry)
    lb = loss_budget(pc, pair_stop_loss_pct)
    pb = profit_budget(pc, pair_take_profit_pct)
    desired = desired_net_profit_per_pair(
        pc,
        pair_stop_loss_pct=pair_stop_loss_pct,
        pair_take_profit_pct=pair_take_profit_pct,
    )
    yes_planned = planned_stop_price(yes_entry, lb)
    no_planned = planned_stop_price(no_entry, lb)
    return PairPnLBudgets(
        pair_cost=pc,
        loss_budget=lb,
        profit_budget=pb,
        desired_net_profit_per_pair=desired,
        yes_planned_stop=yes_planned,
        yes_trigger_stop=trigger_stop_price(yes_planned, slippage_buffer),
        no_planned_stop=no_planned,
        no_trigger_stop=trigger_stop_price(no_planned, slippage_buffer),
    )


def apply_budgets_to_state(state: PairedBinaryRuntimeState, budgets: PairPnLBudgets) -> None:
    state.pair_cost = budgets.pair_cost
    state.loss_budget = budgets.loss_budget
    state.profit_budget = budgets.profit_budget
    state.desired_net_profit_per_pair = budgets.desired_net_profit_per_pair
    state.yes_planned_stop = budgets.yes_planned_stop
    state.yes_trigger_stop = budgets.yes_trigger_stop
    state.no_planned_stop = budgets.no_planned_stop
    state.no_trigger_stop = budgets.no_trigger_stop


def static_survivor_target(
    survivor_entry: Decimal,
    profit_budget_value: Decimal,
    slippage_buffer: Decimal,
) -> SurvivorTargetPlan:
    planned = planned_target_price(survivor_entry, profit_budget_value)
    return SurvivorTargetPlan(
        planned_target=planned,
        trigger_target=trigger_target_price(planned, slippage_buffer),
    )


def reprice_survivor_target_after_loser_exit(
    *,
    survivor_entry: Decimal,
    loser_entry: Decimal,
    loser_exit_fill: Decimal,
    pair_cost: Decimal,
    pair_stop_loss_pct: Decimal,
    pair_take_profit_pct: Decimal,
    slippage_buffer: Decimal,
) -> SurvivorTargetPlan:
    realized = loser_entry - loser_exit_fill
    desired = desired_net_profit_per_pair(
        pair_cost,
        pair_stop_loss_pct=pair_stop_loss_pct,
        pair_take_profit_pct=pair_take_profit_pct,
    )
    required = realized + desired
    planned = survivor_entry + required
    return SurvivorTargetPlan(
        planned_target=planned,
        trigger_target=trigger_target_price(planned, slippage_buffer),
        realized_loser_loss=realized,
        required_winner_gain=required,
    )


def spread_exceeds_loss_budget(
    spread: Decimal,
    estimated_loss_budget: Decimal,
    slippage_buffer: Decimal,
) -> bool:
    return spread + slippage_buffer > estimated_loss_budget


def activation_gap_exceeds_loss_budget(
    entry: Decimal,
    activation_bid: Decimal,
    actual_loss_budget: Decimal,
    slippage_buffer: Decimal,
) -> bool:
    gap = entry - activation_bid
    return gap + slippage_buffer > actual_loss_budget


def expected_pnl_total(state: PairedBinaryRuntimeState) -> Decimal | None:
    if state.desired_net_profit_per_pair is None:
        return None
    return state.desired_net_profit_per_pair * state.effective_qty


@dataclass(frozen=True)
class RealizedCashflowPnL:
    yes_entry_cash: Decimal
    no_entry_cash: Decimal
    yes_exit_cash: Decimal
    no_exit_cash: Decimal
    yes_entry_qty: Decimal
    no_entry_qty: Decimal
    yes_exit_qty: Decimal
    no_exit_qty: Decimal
    buy_cash_total: Decimal
    sell_cash_total: Decimal
    pnl_total: Decimal
    pnl_per_pair: Decimal
    effective_pair_qty: Decimal


def realized_pnl_from_cashflows(state: PairedBinaryRuntimeState) -> RealizedCashflowPnL | None:
    """Authoritative realized PnL from stored matched cashflows only."""
    from tyrex_pm.strategies.paired_binary.state import leg_entry_avg_price, leg_exit_avg_price

    yes = state.yes
    no = state.no
    required = (
        yes.entry_cash,
        no.entry_cash,
        yes.exit_cash,
        no.exit_cash,
        yes.entry_qty,
        no.entry_qty,
        yes.exit_qty,
        no.exit_qty,
    )
    if any(v is None for v in required):
        return None
    assert yes.entry_cash is not None and no.entry_cash is not None
    assert yes.exit_cash is not None and no.exit_cash is not None
    assert yes.entry_qty is not None and no.entry_qty is not None
    assert yes.exit_qty is not None and no.exit_qty is not None

    buy_cash_total = yes.entry_cash + no.entry_cash
    sell_cash_total = yes.exit_cash + no.exit_cash
    effective = state.effective_qty
    if effective <= 0:
        effective = min(yes.entry_qty, no.entry_qty, yes.exit_qty, no.exit_qty)
    if effective <= 0:
        return None
    pnl_total = sell_cash_total - buy_cash_total
    pnl_per_pair = pnl_total / effective
    _ = leg_entry_avg_price, leg_exit_avg_price  # derived at emit time
    return RealizedCashflowPnL(
        yes_entry_cash=yes.entry_cash,
        no_entry_cash=no.entry_cash,
        yes_exit_cash=yes.exit_cash,
        no_exit_cash=no.exit_cash,
        yes_entry_qty=yes.entry_qty,
        no_entry_qty=no.entry_qty,
        yes_exit_qty=yes.exit_qty,
        no_exit_qty=no.exit_qty,
        buy_cash_total=buy_cash_total,
        sell_cash_total=sell_cash_total,
        pnl_total=pnl_total,
        pnl_per_pair=pnl_per_pair,
        effective_pair_qty=effective,
    )


def price_based_pnl_estimate_from_stored_prices(
    *,
    yes_entry: Decimal,
    no_entry: Decimal,
    yes_exit: Decimal | None,
    no_exit: Decimal | None,
    qty: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
    """Non-authoritative diagnostic: per-share price reconstruction only."""
    if yes_exit is None or no_exit is None:
        return None
    pc = pair_cost_from_entries(yes_entry, no_entry)
    exit_value = yes_exit + no_exit
    pnl_per_pair = exit_value - pc
    pnl_total = pnl_per_pair * qty
    return pc, exit_value, pnl_per_pair, pnl_total


# Backward-compatible alias for tests/docs referencing the old name.
realized_pnl_from_exits = price_based_pnl_estimate_from_stored_prices


def budgets_from_cfg(
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
) -> PairPnLBudgets | None:
    if state.yes_entry is None or state.no_entry is None:
        return None
    return compute_pair_pnl_budgets(
        state.yes_entry,
        state.no_entry,
        pair_stop_loss_pct=cfg.pair_stop_loss_pct,
        pair_take_profit_pct=cfg.pair_take_profit_pct,
        slippage_buffer=cfg.slippage_buffer,
    )
