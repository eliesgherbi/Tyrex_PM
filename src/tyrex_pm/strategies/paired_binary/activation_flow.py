"""Activation safety recheck and emergency unwind orchestration."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.time import monotonic_s
from tyrex_pm.runtime.config import PairedBinaryStrategyConfig
from tyrex_pm.strategies.paired_binary.entry_eval import LegBook
from tyrex_pm.strategies.paired_binary.exit_engine import (
    ActivationSafetyResult,
    check_activation_loss_budget,
    ensure_pnl_budgets,
)
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState


def begin_activation_recheck(state: PairedBinaryRuntimeState) -> None:
    state.phase = PairedBinaryPhase.ACTIVATION_PENDING_RECHECK
    state.activation_recheck_started_ts = monotonic_s()
    state.activation_recheck_attempts = 0


def activation_recheck_elapsed_s(state: PairedBinaryRuntimeState) -> float:
    if state.activation_recheck_started_ts is None:
        return 0.0
    return monotonic_s() - state.activation_recheck_started_ts


def activation_recheck_expired(state: PairedBinaryRuntimeState, cfg: PairedBinaryStrategyConfig) -> bool:
    if cfg.activation_gap_retry_s <= 0:
        return True
    return activation_recheck_elapsed_s(state) >= cfg.activation_gap_retry_s


def should_use_activation_recheck(cfg: PairedBinaryStrategyConfig) -> bool:
    return cfg.activation_gap_retry_s > 0


def evaluate_activation_safety(
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
    yes_book: LegBook,
    no_book: LegBook,
) -> ActivationSafetyResult:
    ensure_pnl_budgets(state, cfg)
    return check_activation_loss_budget(state, cfg, yes_book, no_book)


def leg_inventory_qty(coord, cfg: PairedBinaryStrategyConfig, leg: str) -> Decimal:
    from tyrex_pm.core.ids import TokenId

    ledger = coord.allocation_ledger
    if ledger is None:
        return Decimal("0")
    tid = cfg.yes_token_id if leg == "yes" else cfg.no_token_id
    return ledger.get_available_allocated(cfg.owner_id, TokenId(tid))
