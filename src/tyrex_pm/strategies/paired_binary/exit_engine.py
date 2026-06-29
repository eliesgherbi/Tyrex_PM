"""Paired binary exit lifecycle — sellability, pair-PnL stops, trigger-pending (Phase 4.6)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.runtime.config import PairedBinaryStrategyConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.intent_work import IntentWorkUnit
from tyrex_pm.strategies.paired_binary.entry_eval import LegBook
from tyrex_pm.strategies.paired_binary.lifecycle import choose_dual_stop_leg
from tyrex_pm.strategies.paired_binary.pnl import (
    apply_budgets_to_state,
    budgets_from_cfg,
    reprice_survivor_target_after_loser_exit,
    static_survivor_target,
)
from tyrex_pm.strategies.paired_binary.sizing import build_exit_work_unit, clamp_exit_size
from tyrex_pm.strategies.paired_binary.state import (
    LegRuntime,
    PairedBinaryPhase,
    PairedBinaryRuntimeState,
)

TriggerType = Literal["stop_loss", "take_profit", "timeout"]
ExitLeg = Literal["yes", "no"]
BlockReason = Literal[
    "NO_SELLABLE_INVENTORY",
    "SIZING_ZERO",
    "BOOK_STALE",
    "RISK_DENIED",
    "PLANNER_DENIED",
    "OMS_REJECTED",
]


@dataclass(frozen=True)
class LegSellability:
    allocation_qty: Decimal
    venue_available_qty: Decimal
    sellable_qty: Decimal


@dataclass(frozen=True)
class ExitTriggerContext:
    leg: ExitLeg
    trigger_type: TriggerType
    trigger_price: Decimal
    reference_price: Decimal
    target_price: Decimal | None
    planned_qty: Decimal
    allocation_qty: Decimal
    venue_available_qty: Decimal
    final_size: Decimal
    reason: BlockReason | None
    planned_stop_price: Decimal | None = None
    trigger_stop_price: Decimal | None = None
    planned_target_price: Decimal | None = None
    trigger_target_price: Decimal | None = None


@dataclass(frozen=True)
class ExitBuildResult:
    work: IntentWorkUnit | None
    ctx: ExitTriggerContext
    blocked: bool


@dataclass(frozen=True)
class ActivationSafetyResult:
    ok: bool
    reason: str | None = None


def leg_runtime(state: PairedBinaryRuntimeState, leg: ExitLeg) -> LegRuntime:
    return state.yes if leg == "yes" else state.no


def pending_phase_for_leg(leg: ExitLeg, trigger_type: TriggerType) -> PairedBinaryPhase:
    if trigger_type == "take_profit":
        return PairedBinaryPhase.TP_PENDING_YES if leg == "yes" else PairedBinaryPhase.TP_PENDING_NO
    if trigger_type == "timeout":
        return PairedBinaryPhase.TIMEOUT_PENDING
    return PairedBinaryPhase.STOP_PENDING_YES if leg == "yes" else PairedBinaryPhase.STOP_PENDING_NO


def exiting_phase_for_leg(leg: ExitLeg) -> PairedBinaryPhase:
    return {
        "yes": PairedBinaryPhase.EXITING_YES,
        "no": PairedBinaryPhase.EXITING_NO,
    }[leg]


def token_for_leg(cfg: PairedBinaryStrategyConfig, leg: ExitLeg) -> TokenId:
    return TokenId(cfg.yes_token_id if leg == "yes" else cfg.no_token_id)


def leg_sellability(
    coord: RuntimeCoordinator,
    *,
    owner_id: str,
    token_id: TokenId,
    planned: Decimal,
    exit_order_style,
) -> LegSellability:
    sizing = clamp_exit_size(
        coord,
        owner_id=owner_id,
        token_id=token_id,
        planned=planned,
        exit_order_style=exit_order_style,
    )
    return LegSellability(
        allocation_qty=sizing.owner_allocation,
        venue_available_qty=sizing.venue_available,
        sellable_qty=sizing.final_size,
    )


def both_legs_sellable(
    coord: RuntimeCoordinator,
    cfg: PairedBinaryStrategyConfig,
    state: PairedBinaryRuntimeState,
) -> tuple[bool, LegSellability, LegSellability]:
    eq = state.effective_qty
    yes = leg_sellability(
        coord,
        owner_id=cfg.owner_id,
        token_id=TokenId(cfg.yes_token_id),
        planned=eq,
        exit_order_style=cfg.exit_order_style,
    )
    no = leg_sellability(
        coord,
        owner_id=cfg.owner_id,
        token_id=TokenId(cfg.no_token_id),
        planned=eq,
        exit_order_style=cfg.exit_order_style,
    )
    min_eff = cfg.min_effective_pair_qty or eq
    ok = (
        yes.allocation_qty >= min_eff
        and no.allocation_qty >= min_eff
        and yes.sellable_qty >= eq
        and no.sellable_qty >= eq
    )
    return ok, yes, no


def ensure_pnl_budgets(
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
) -> bool:
    if state.yes_trigger_stop is not None and state.no_trigger_stop is not None:
        return True
    budgets = budgets_from_cfg(state, cfg)
    if budgets is None:
        return False
    apply_budgets_to_state(state, budgets)
    state.expected_pnl_total = state.desired_net_profit_per_pair * state.effective_qty
    return True


def check_activation_loss_budget(
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
    yes_book: LegBook,
    no_book: LegBook,
) -> ActivationSafetyResult:
    from tyrex_pm.core import reason_codes as rc
    from tyrex_pm.strategies.paired_binary.pnl import activation_gap_exceeds_loss_budget

    if not cfg.reject_if_spread_exceeds_loss_budget:
        return ActivationSafetyResult(ok=True)
    if (
        state.yes_entry is None
        or state.no_entry is None
        or state.loss_budget is None
        or yes_book.bid is None
        or no_book.bid is None
    ):
        return ActivationSafetyResult(ok=True)

    yes_gap = state.yes_entry - yes_book.bid
    no_gap = state.no_entry - no_book.bid
    if activation_gap_exceeds_loss_budget(
        state.yes_entry, yes_book.bid, state.loss_budget, cfg.slippage_buffer
    ):
        return ActivationSafetyResult(ok=False, reason=rc.YES_ACTIVATION_GAP_EXCEEDS_LOSS_BUDGET)
    if activation_gap_exceeds_loss_budget(
        state.no_entry, no_book.bid, state.loss_budget, cfg.slippage_buffer
    ):
        return ActivationSafetyResult(ok=False, reason=rc.NO_ACTIVATION_GAP_EXCEEDS_LOSS_BUDGET)
    return ActivationSafetyResult(ok=True)


def prepare_yes_stop_trigger(
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
) -> None:
    state.yes.triggered = True
    state.yes.pending_trigger_type = "stop_loss"
    if state.no_entry is not None and state.profit_budget is not None:
        plan = static_survivor_target(state.no_entry, state.profit_budget, cfg.slippage_buffer)
        state.no_planned_target = plan.planned_target
        state.no_target = plan.trigger_target
    state.phase = PairedBinaryPhase.STOP_PENDING_YES


def prepare_no_stop_trigger(
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
) -> None:
    state.no.triggered = True
    state.no.pending_trigger_type = "stop_loss"
    if state.yes_entry is not None and state.profit_budget is not None:
        plan = static_survivor_target(state.yes_entry, state.profit_budget, cfg.slippage_buffer)
        state.yes_planned_target = plan.planned_target
        state.yes_target = plan.trigger_target
    state.phase = PairedBinaryPhase.STOP_PENDING_NO


def reprice_survivor_after_loser_exit(
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
    *,
    loser_leg: ExitLeg,
    loser_exit_fill: Decimal,
) -> tuple[Decimal | None, Decimal | None]:
    """Reprice survivor target using realized loser loss. Returns (old_target, new_target)."""
    if state.pair_cost is None or state.yes_entry is None or state.no_entry is None:
        return None, None

    if loser_leg == "yes":
        survivor_entry = state.no_entry
        loser_entry = state.yes_entry
        old = state.no_target
        plan = reprice_survivor_target_after_loser_exit(
            survivor_entry=survivor_entry,
            loser_entry=loser_entry,
            loser_exit_fill=loser_exit_fill,
            pair_cost=state.pair_cost,
            pair_stop_loss_pct=cfg.pair_stop_loss_pct,
            pair_take_profit_pct=cfg.pair_take_profit_pct,
            slippage_buffer=cfg.slippage_buffer,
        )
        state.no_planned_target = plan.planned_target
        state.no_target = plan.trigger_target
        return old, plan.trigger_target

    survivor_entry = state.yes_entry
    loser_entry = state.no_entry
    old = state.yes_target
    plan = reprice_survivor_target_after_loser_exit(
        survivor_entry=survivor_entry,
        loser_entry=loser_entry,
        loser_exit_fill=loser_exit_fill,
        pair_cost=state.pair_cost,
        pair_stop_loss_pct=cfg.pair_stop_loss_pct,
        pair_take_profit_pct=cfg.pair_take_profit_pct,
        slippage_buffer=cfg.slippage_buffer,
    )
    state.yes_planned_target = plan.planned_target
    state.yes_target = plan.trigger_target
    return old, plan.trigger_target


def prepare_tp_trigger(state: PairedBinaryRuntimeState, leg: ExitLeg) -> None:
    rt = leg_runtime(state, leg)
    rt.pending_trigger_type = "take_profit"
    state.phase = pending_phase_for_leg(leg, "take_profit")


def prepare_timeout_trigger(
    state: PairedBinaryRuntimeState,
    *,
    legs: tuple[ExitLeg, ...],
) -> None:
    for leg in legs:
        leg_runtime(state, leg).pending_trigger_type = "timeout"
    state.pending_timeout_legs = list(legs)
    state.phase = PairedBinaryPhase.TIMEOUT_PENDING


def sizing_block_reason(sizing: LegSellability, *, book_stale: bool) -> BlockReason | None:
    if book_stale:
        return "BOOK_STALE"
    if sizing.venue_available_qty <= 0 and sizing.allocation_qty > 0:
        return "NO_SELLABLE_INVENTORY"
    if sizing.sellable_qty <= 0:
        return "SIZING_ZERO"
    return None


def _stop_context_prices(
    state: PairedBinaryRuntimeState,
    leg: ExitLeg,
) -> tuple[Decimal | None, Decimal | None]:
    if leg == "yes":
        return state.yes_planned_stop, state.yes_trigger_stop
    return state.no_planned_stop, state.no_trigger_stop


def _target_context_prices(
    state: PairedBinaryRuntimeState,
    leg: ExitLeg,
) -> tuple[Decimal | None, Decimal | None]:
    if leg == "yes":
        return state.yes_planned_target, state.yes_target
    return state.no_planned_target, state.no_target


def try_build_exit(
    coord: RuntimeCoordinator,
    cfg: PairedBinaryStrategyConfig,
    state: PairedBinaryRuntimeState,
    *,
    leg: ExitLeg,
    trigger_type: TriggerType,
    bid: Decimal,
    qty: Decimal,
    pair_id: str,
    book_stale: bool = False,
    target_price: Decimal | None = None,
) -> ExitBuildResult:
    owner = cfg.owner_id
    token_id = token_for_leg(cfg, leg)
    leg_rt = leg_runtime(state, leg)
    planned_stop, trigger_stop = _stop_context_prices(state, leg)
    planned_target, trigger_target = _target_context_prices(state, leg)
    ref = state.yes_entry if leg == "yes" else state.no_entry

    if leg_rt.exit_submitted:
        ctx = ExitTriggerContext(
            leg=leg,
            trigger_type=trigger_type,
            trigger_price=bid,
            reference_price=ref or Decimal("0"),
            target_price=target_price,
            planned_stop_price=planned_stop,
            trigger_stop_price=trigger_stop,
            planned_target_price=planned_target,
            trigger_target_price=trigger_target,
            planned_qty=qty,
            allocation_qty=Decimal("0"),
            venue_available_qty=Decimal("0"),
            final_size=Decimal("0"),
            reason=None,
        )
        return ExitBuildResult(work=None, ctx=ctx, blocked=False)

    sizing = leg_sellability(
        coord,
        owner_id=owner,
        token_id=token_id,
        planned=qty,
        exit_order_style=cfg.exit_order_style,
    )
    block = sizing_block_reason(sizing, book_stale=book_stale)
    ctx = ExitTriggerContext(
        leg=leg,
        trigger_type=trigger_type,
        trigger_price=bid,
        reference_price=ref or Decimal("0"),
        target_price=target_price,
        planned_stop_price=planned_stop,
        trigger_stop_price=trigger_stop,
        planned_target_price=planned_target,
        trigger_target_price=trigger_target,
        planned_qty=qty,
        allocation_qty=sizing.allocation_qty,
        venue_available_qty=sizing.venue_available_qty,
        final_size=sizing.sellable_qty,
        reason=block,
    )
    if block is not None:
        return ExitBuildResult(work=None, ctx=ctx, blocked=True)

    leg_rt.last_exit_bid = bid
    leg_corr = leg_rt.leg_correlation_id or f"{pair_id}:{leg}:exit"
    work = build_exit_work_unit(
        token_id=token_id,
        size=sizing.sellable_qty,
        limit_price=bid,
        order_style=cfg.exit_order_style,
        owner_id=owner,
        pair_correlation_id=pair_id,
        leg=leg,
        leg_correlation_id=leg_corr,
        reason=trigger_type if trigger_type != "take_profit" else "take_profit",
        sizing=clamp_exit_size(
            coord,
            owner_id=owner,
            token_id=token_id,
            planned=qty,
            exit_order_style=cfg.exit_order_style,
        ),
    )
    return ExitBuildResult(work=work, ctx=ctx, blocked=work is None)


def confirm_exit_submitted(state: PairedBinaryRuntimeState, leg: ExitLeg) -> PairedBinaryPhase:
    """Transition to EXITING_* after pipeline accepted the SELL order."""
    rt = leg_runtime(state, leg)
    rt.exit_submitted = True
    rt.pending_trigger_type = None
    rt.pending_trigger_reason = None

    other_leg: ExitLeg = "no" if leg == "yes" else "yes"
    other_rt = leg_runtime(state, other_leg)

    if state.phase == PairedBinaryPhase.TIMEOUT_PENDING:
        pending = state.pending_timeout_legs or []
        if leg_runtime(state, leg).exit_submitted:
            if all(leg_runtime(state, lg).exit_submitted for lg in pending):
                state.phase = PairedBinaryPhase.EXITING_BOTH
                state.pending_timeout_legs = None
                return state.phase
        state.phase = exiting_phase_for_leg(leg)
        return state.phase

    if state.phase in {
        PairedBinaryPhase.STOP_PENDING_YES,
        PairedBinaryPhase.STOP_PENDING_NO,
        PairedBinaryPhase.TP_PENDING_YES,
        PairedBinaryPhase.TP_PENDING_NO,
        PairedBinaryPhase.ONLY_YES_ACTIVE,
        PairedBinaryPhase.ONLY_NO_ACTIVE,
        PairedBinaryPhase.BOTH_LEGS_ACTIVE,
    }:
        state.phase = exiting_phase_for_leg(leg)
        return state.phase

    if state.phase == PairedBinaryPhase.EXITING_BOTH:
        return state.phase
    if other_rt.exit_submitted and rt.exit_submitted:
        state.phase = PairedBinaryPhase.EXITING_BOTH
    else:
        state.phase = exiting_phase_for_leg(leg)
    return state.phase


def record_exit_blocked(
    state: PairedBinaryRuntimeState,
    leg: ExitLeg,
    *,
    reason: BlockReason,
    trigger_type: TriggerType,
) -> None:
    rt = leg_runtime(state, leg)
    rt.pending_trigger_reason = reason
    if rt.pending_trigger_type is None:
        rt.pending_trigger_type = trigger_type
    if state.phase not in {
        PairedBinaryPhase.STOP_PENDING_YES,
        PairedBinaryPhase.STOP_PENDING_NO,
        PairedBinaryPhase.TP_PENDING_YES,
        PairedBinaryPhase.TP_PENDING_NO,
        PairedBinaryPhase.TIMEOUT_PENDING,
    }:
        state.phase = pending_phase_for_leg(leg, trigger_type)


def evaluate_dual_stop(
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
    yes_book: LegBook,
    no_book: LegBook,
) -> ExitLeg | None:
    if not ensure_pnl_budgets(state, cfg):
        return None
    if yes_book.bid is None or no_book.bid is None:
        return None
    if state.yes_trigger_stop is None or state.no_trigger_stop is None:
        return None
    if state.yes_entry is None or state.no_entry is None:
        return None

    yes_hit = yes_book.bid <= state.yes_trigger_stop
    no_hit = no_book.bid <= state.no_trigger_stop
    if not yes_hit and not no_hit:
        return None
    if yes_hit and no_hit:
        yes_loss = state.yes_entry - yes_book.bid
        no_loss = state.no_entry - no_book.bid
        yes_spread = (yes_book.ask - yes_book.bid) if yes_book.ask and yes_book.bid else Decimal("0")
        no_spread = (no_book.ask - no_book.bid) if no_book.ask and no_book.bid else Decimal("0")
        first = choose_dual_stop_leg(
            yes_loss=yes_loss,
            no_loss=no_loss,
            yes_spread=yes_spread,
            no_spread=no_spread,
        )
        return "yes" if first == "yes" else "no"
    if yes_hit:
        return "yes"
    return "no"


MONITOR_TICK_PHASES = frozenset(
    {
        PairedBinaryPhase.BOTH_LEGS_ACTIVE,
        PairedBinaryPhase.ONLY_YES_ACTIVE,
        PairedBinaryPhase.ONLY_NO_ACTIVE,
        PairedBinaryPhase.STOP_PENDING_YES,
        PairedBinaryPhase.STOP_PENDING_NO,
        PairedBinaryPhase.TP_PENDING_YES,
        PairedBinaryPhase.TP_PENDING_NO,
        PairedBinaryPhase.TIMEOUT_PENDING,
        PairedBinaryPhase.EXITING_YES,
        PairedBinaryPhase.EXITING_NO,
        PairedBinaryPhase.EXITING_BOTH,
    }
)
