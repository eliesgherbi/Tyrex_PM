"""Startup recovery for paired binary positions (Phase 4.6 + Phase 2 market-aware identity)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from tyrex_pm.core.enums import ExecutionMode, Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import PairedBinaryRuntimeConfig, PairedBinaryStrategyConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.paired_binary_shutdown import OPEN_EXPOSURE_PHASES
from tyrex_pm.strategies.paired_binary import facts as pb_facts
from tyrex_pm.strategies.paired_binary.entry_eval import read_leg_book
from tyrex_pm.strategies.paired_binary.state import (
    TERMINAL_PHASES,
    PairedBinaryPhase,
    PairedBinaryRuntimeState,
    load_persisted_state,
    persistence_path,
    save_persisted_state,
)

PAIR_ABORTED_FLAT_SAGA = "PAIR_ABORTED_FLAT"


def _entry_from_venue(coord: RuntimeCoordinator, token_id: TokenId) -> Decimal | None:
    pos = coord.wallet.positions.get(token_id)
    if pos is None or pos.qty <= 0:
        return None
    return pos.avg_price_usd


def _venue_qty(coord: RuntimeCoordinator, token_id: TokenId) -> Decimal:
    pos = coord.wallet.positions.get(token_id)
    if pos is None:
        return Decimal("0")
    return max(Decimal("0"), pos.qty)


def _has_open_sell(coord: RuntimeCoordinator, token_id: TokenId) -> bool:
    for lo in coord.orders.orders.values():
        if lo.token_id == token_id and lo.side == Side.SELL and lo.remaining > 0:
            return True
    return False


def _fresh_idle_state(cfg: PairedBinaryStrategyConfig) -> PairedBinaryRuntimeState:
    return PairedBinaryRuntimeState(
        owner_id=cfg.owner_id,
        market_id=cfg.market_id,
        yes_token_id=cfg.yes_token_id,
        no_token_id=cfg.no_token_id,
        phase=PairedBinaryPhase.IDLE,
    )


def _sync_cfg_fields(state: PairedBinaryRuntimeState, cfg: PairedBinaryStrategyConfig) -> None:
    state.owner_id = cfg.owner_id
    state.market_id = cfg.market_id
    state.yes_token_id = cfg.yes_token_id
    state.no_token_id = cfg.no_token_id


def _token_pair_match_status(
    persisted: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
) -> str:
    py = str(persisted.yes_token_id or "").strip()
    pn = str(persisted.no_token_id or "").strip()
    if not py or not pn:
        return "missing_token_identity"
    if py != cfg.yes_token_id or pn != cfg.no_token_id:
        return "mismatch"
    return "match"


def _is_terminal_lifecycle(persisted: PairedBinaryRuntimeState) -> bool:
    if persisted.phase in TERMINAL_PHASES:
        return True
    return persisted.pair_entry_saga_phase == PAIR_ABORTED_FLAT_SAGA


def _recovery_identity_payload(
    persisted: PairedBinaryRuntimeState | None,
    cfg: PairedBinaryStrategyConfig,
    *,
    strategy_hash: str | None,
    scenario_hash: str | None,
    execution_mode: ExecutionMode | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "current_yes_token_id": cfg.yes_token_id,
        "current_no_token_id": cfg.no_token_id,
        "persisted_yes_token_id": persisted.yes_token_id if persisted else None,
        "persisted_no_token_id": persisted.no_token_id if persisted else None,
        "persisted_state": persisted.phase.value if persisted else None,
        "terminal_state": persisted.phase.value if persisted and _is_terminal_lifecycle(persisted) else None,
        "strategy_hash": strategy_hash,
        "scenario_hash": scenario_hash,
    }
    if execution_mode is not None:
        payload["run_mode"] = execution_mode.value
    return payload


def _bootstrap_from_wallet(
    state: PairedBinaryRuntimeState,
    coord: RuntimeCoordinator,
    cfg: PairedBinaryStrategyConfig,
    yes_tid: TokenId,
    no_tid: TokenId,
) -> tuple[str, str | None]:
    """Apply venue/allocation bootstrap independent of persisted lifecycle. Returns (source, action)."""
    ledger = coord.allocation_ledger
    yes_qty = Decimal("0")
    no_qty = Decimal("0")
    yes_venue = _venue_qty(coord, yes_tid)
    no_venue = _venue_qty(coord, no_tid)
    if ledger is not None:
        yes_qty = ledger.get_available_allocated(cfg.owner_id, yes_tid)
        no_qty = ledger.get_available_allocated(cfg.owner_id, no_tid)

    recovery_source = "venue_reconcile"
    recovery_action: str | None = None

    if yes_venue > 0 and no_venue > 0:
        state.phase = PairedBinaryPhase.BOTH_LEGS_FILLED
        state.effective_qty = min(yes_venue, no_venue)
        state.yes_entry = _entry_from_venue(coord, yes_tid)
        state.no_entry = _entry_from_venue(coord, no_tid)
        state.entry_price_source = "venue_avg"
        recovery_action = "bootstrap_both_from_venue"
    elif yes_venue > 0:
        state.phase = PairedBinaryPhase.ONLY_YES_ACTIVE
        state.effective_qty = yes_venue
        state.yes_entry = _entry_from_venue(coord, yes_tid)
        state.entry_price_source = "venue_avg"
        recovery_action = "bootstrap_yes_from_venue"
    elif no_venue > 0:
        state.phase = PairedBinaryPhase.ONLY_NO_ACTIVE
        state.effective_qty = no_venue
        state.no_entry = _entry_from_venue(coord, no_tid)
        state.entry_price_source = "venue_avg"
        recovery_action = "bootstrap_no_from_venue"
    elif yes_qty > 0 and no_qty > 0:
        state.phase = PairedBinaryPhase.BOTH_LEGS_FILLED
        state.effective_qty = min(yes_qty, no_qty)
        recovery_source = "allocation_reconcile"
        recovery_action = "bootstrap_both_from_ledger"
    elif yes_qty > 0:
        state.phase = PairedBinaryPhase.ONLY_YES_ACTIVE
        state.effective_qty = yes_qty
        recovery_source = "allocation_reconcile"
        recovery_action = "bootstrap_yes_from_ledger"
    elif no_qty > 0:
        state.phase = PairedBinaryPhase.ONLY_NO_ACTIVE
        state.effective_qty = no_qty
        recovery_source = "allocation_reconcile"
        recovery_action = "bootstrap_no_from_ledger"
    else:
        state.phase = PairedBinaryPhase.IDLE

    return recovery_source, recovery_action


def _apply_persisted_entry_prices(
    state: PairedBinaryRuntimeState,
    coord: RuntimeCoordinator,
    yes_tid: TokenId,
    no_tid: TokenId,
) -> None:
    yes_venue = _venue_qty(coord, yes_tid)
    no_venue = _venue_qty(coord, no_tid)
    if state.yes_entry is None and yes_venue > 0:
        state.yes_entry = _entry_from_venue(coord, yes_tid)
        if state.yes_entry is not None:
            state.entry_price_source = "venue_avg"
    if state.no_entry is None and no_venue > 0:
        state.no_entry = _entry_from_venue(coord, no_tid)
        if state.no_entry is not None and state.entry_price_source is None:
            state.entry_price_source = "venue_avg"


def _recover_exiting_phase(
    state: PairedBinaryRuntimeState,
    coord: RuntimeCoordinator,
    yes_tid: TokenId,
    no_tid: TokenId,
) -> str | None:
    """Resolve stuck EXITING_* into actionable phase. Returns recovery_action label."""
    phase = state.phase
    if phase == PairedBinaryPhase.EXITING_YES:
        yes_qty = _venue_qty(coord, yes_tid)
        no_qty = _venue_qty(coord, no_tid)
        if yes_qty <= 0:
            if no_qty > 0:
                state.phase = PairedBinaryPhase.ONLY_NO_ACTIVE
                state.effective_qty = no_qty
                return "exiting_yes_flat_to_only_no"
            state.phase = PairedBinaryPhase.DONE
            return "exiting_yes_flat_to_done"
        if not _has_open_sell(coord, yes_tid):
            state.phase = PairedBinaryPhase.STOP_PENDING_YES
            state.yes.pending_trigger_type = state.yes.pending_trigger_type or "timeout"
            state.effective_qty = min(yes_qty, no_qty) if no_qty > 0 else yes_qty
            return "exiting_yes_retry_stop_pending"
        return "exiting_yes_monitor_sell"

    if phase == PairedBinaryPhase.EXITING_NO:
        yes_qty = _venue_qty(coord, yes_tid)
        no_qty = _venue_qty(coord, no_tid)
        if no_qty <= 0:
            if yes_qty > 0:
                state.phase = PairedBinaryPhase.ONLY_YES_ACTIVE
                state.effective_qty = yes_qty
                return "exiting_no_flat_to_only_yes"
            state.phase = PairedBinaryPhase.DONE
            return "exiting_no_flat_to_done"
        if not _has_open_sell(coord, no_tid):
            state.phase = PairedBinaryPhase.STOP_PENDING_NO
            state.no.pending_trigger_type = state.no.pending_trigger_type or "timeout"
            state.effective_qty = min(yes_qty, no_qty) if yes_qty > 0 else no_qty
            return "exiting_no_retry_stop_pending"
        return "exiting_no_monitor_sell"

    if phase == PairedBinaryPhase.EXITING_BOTH:
        yes_qty = _venue_qty(coord, yes_tid)
        no_qty = _venue_qty(coord, no_tid)
        if yes_qty <= 0 and no_qty <= 0:
            state.phase = PairedBinaryPhase.DONE
            return "exiting_both_flat_to_done"
        if yes_qty <= 0 and no_qty > 0:
            state.phase = PairedBinaryPhase.ONLY_NO_ACTIVE
            state.effective_qty = no_qty
            return "exiting_both_yes_flat_only_no"
        if no_qty <= 0 and yes_qty > 0:
            state.phase = PairedBinaryPhase.ONLY_YES_ACTIVE
            state.effective_qty = yes_qty
            return "exiting_both_no_flat_only_yes"
        open_yes = _has_open_sell(coord, yes_tid)
        open_no = _has_open_sell(coord, no_tid)
        if not open_yes and not open_no:
            state.phase = PairedBinaryPhase.TIMEOUT_PENDING
            state.pending_timeout_legs = ["yes", "no"]
            state.yes.pending_trigger_type = state.yes.pending_trigger_type or "timeout"
            state.no.pending_trigger_type = state.no.pending_trigger_type or "timeout"
            state.effective_qty = min(yes_qty, no_qty)
            return "exiting_both_retry_timeout_pending"
        return "exiting_both_monitor_sell"
    return None


def _has_open_buy_entry(coord: RuntimeCoordinator, token_id: TokenId) -> bool:
    for lo in coord.orders.orders.values():
        if lo.token_id == token_id and lo.side == Side.BUY and lo.remaining > 0:
            return True
    return False


def _reconcile_recovered_state(
    state: PairedBinaryRuntimeState,
    coord: RuntimeCoordinator,
    cfg: PairedBinaryStrategyConfig,
    yes_tid: TokenId,
    no_tid: TokenId,
    recovery_action: str | None,
) -> str | None:
    ledger = coord.allocation_ledger
    yes_qty = Decimal("0")
    no_qty = Decimal("0")
    yes_venue = _venue_qty(coord, yes_tid)
    no_venue = _venue_qty(coord, no_tid)
    if ledger is not None:
        yes_qty = ledger.get_available_allocated(cfg.owner_id, yes_tid)
        no_qty = ledger.get_available_allocated(cfg.owner_id, no_tid)

    if yes_venue <= 0 and no_venue <= 0 and yes_qty <= 0 and no_qty <= 0:
        if state.phase in {
            PairedBinaryPhase.EXITING_YES,
            PairedBinaryPhase.EXITING_NO,
            PairedBinaryPhase.EXITING_BOTH,
        }:
            state.phase = PairedBinaryPhase.DONE
            recovery_action = recovery_action or "exiting_flat_to_done"
        elif state.phase == PairedBinaryPhase.FAILED:
            state.phase = PairedBinaryPhase.IDLE
            recovery_action = recovery_action or "failed_flat_to_idle"
        elif state.phase not in TERMINAL_PHASES:
            state.phase = PairedBinaryPhase.IDLE
            recovery_action = recovery_action or "flat_to_idle"
    elif state.phase in {
        PairedBinaryPhase.EXITING_YES,
        PairedBinaryPhase.EXITING_NO,
        PairedBinaryPhase.EXITING_BOTH,
    }:
        exiting_action = _recover_exiting_phase(state, coord, yes_tid, no_tid)
        if exiting_action is not None:
            recovery_action = exiting_action

    if state.phase == PairedBinaryPhase.BOTH_ENTRY_PENDING:
        open_yes = _has_open_buy_entry(coord, yes_tid)
        open_no = _has_open_buy_entry(coord, no_tid)
        if open_yes or open_no:
            recovery_action = recovery_action or "both_entry_pending_open_orders"
        elif yes_venue > 0 != (no_venue > 0):
            recovery_action = recovery_action or "both_entry_pending_asymmetric"

    if state.phase == PairedBinaryPhase.FAILED and (yes_venue > 0 or no_venue > 0):
        state.phase = PairedBinaryPhase.UNWIND_PENDING
        state.unwind_block_reason = "recovery_failed_non_flat"
        state.effective_qty = max(yes_venue, no_venue, yes_qty, no_qty)
        recovery_action = recovery_action or "failed_non_flat_unwind_pending"

    return recovery_action


def recover_on_startup(
    coord: RuntimeCoordinator,
    cfg: PairedBinaryStrategyConfig,
    *,
    state_dir: Path,
    sink: JsonlSink | None = None,
    run_id: RunId | None = None,
    pb_rt: PairedBinaryRuntimeConfig | None = None,
    execution_mode: ExecutionMode | None = None,
    strategy_hash: str | None = None,
    scenario_hash: str | None = None,
) -> PairedBinaryRuntimeState:
    path = persistence_path(state_dir, cfg.owner_id, cfg.market_id)
    persisted = load_persisted_state(path)
    pb_rt = pb_rt or PairedBinaryRuntimeConfig()
    allow_terminal_resume = pb_rt.allow_terminal_state_resume

    yes_tid = TokenId(cfg.yes_token_id)
    no_tid = TokenId(cfg.no_token_id)

    identity = _recovery_identity_payload(
        persisted,
        cfg,
        strategy_hash=strategy_hash,
        scenario_hash=scenario_hash,
        execution_mode=execution_mode,
    )

    if sink and run_id:
        pb_facts.emit_state_recovery_checked(sink, run_id, identity)

    recovery_source = "venue_reconcile"
    recovery_action: str | None = None
    lifecycle_recovered = False
    applied_reason: str | None = None

    if persisted is None:
        state = _fresh_idle_state(cfg)
        recovery_source, recovery_action = _bootstrap_from_wallet(state, coord, cfg, yes_tid, no_tid)
    else:
        match = _token_pair_match_status(persisted, cfg)

        if match == "missing_token_identity":
            if sink and run_id:
                pb_facts.emit_state_recovery_ignored(
                    sink,
                    run_id,
                    {**identity, "reason": "missing_token_identity"},
                )
            state = _fresh_idle_state(cfg)
            recovery_source, recovery_action = _bootstrap_from_wallet(state, coord, cfg, yes_tid, no_tid)
            if _is_terminal_lifecycle(persisted) and sink and run_id:
                pb_facts.emit_terminal_state_reset(
                    sink,
                    run_id,
                    {**identity, "reason": "missing_token_identity"},
                )
        elif match == "mismatch":
            if sink and run_id:
                pb_facts.emit_state_recovery_ignored(
                    sink,
                    run_id,
                    {**identity, "reason": "token_pair_mismatch"},
                )
            state = _fresh_idle_state(cfg)
            recovery_source, recovery_action = _bootstrap_from_wallet(state, coord, cfg, yes_tid, no_tid)
            if _is_terminal_lifecycle(persisted) and sink and run_id:
                pb_facts.emit_terminal_state_reset(
                    sink,
                    run_id,
                    {**identity, "reason": "token_pair_mismatch"},
                )
        elif _is_terminal_lifecycle(persisted) and not allow_terminal_resume:
            if sink and run_id:
                pb_facts.emit_terminal_state_reset(
                    sink,
                    run_id,
                    {**identity, "reason": "terminal_state_without_explicit_resume"},
                )
            state = _fresh_idle_state(cfg)
            recovery_source, recovery_action = _bootstrap_from_wallet(state, coord, cfg, yes_tid, no_tid)
        elif _is_terminal_lifecycle(persisted) and allow_terminal_resume:
            state = persisted
            _sync_cfg_fields(state, cfg)
            recovery_source = "persisted"
            lifecycle_recovered = True
            applied_reason = "terminal_state_resume"
            _apply_persisted_entry_prices(state, coord, yes_tid, no_tid)
        else:
            state = persisted
            _sync_cfg_fields(state, cfg)
            recovery_source = "persisted"
            lifecycle_recovered = True
            if state.phase in OPEN_EXPOSURE_PHASES:
                applied_reason = "valid_open_exposure_recovery"
            else:
                applied_reason = "persisted_lifecycle"
            _apply_persisted_entry_prices(state, coord, yes_tid, no_tid)

    recovery_action = _reconcile_recovered_state(
        state, coord, cfg, yes_tid, no_tid, recovery_action
    )

    if lifecycle_recovered and applied_reason and sink and run_id:
        pb_facts.emit_state_recovery_applied(
            sink,
            run_id,
            {**identity, "reason": applied_reason, "persisted_state": persisted.phase.value if persisted else None},
        )

    if sink and run_id:
        yes_book = read_leg_book(coord.market_state, yes_tid, max_book_age_s=cfg.max_book_age_s)
        no_book = read_leg_book(coord.market_state, no_tid, max_book_age_s=cfg.max_book_age_s)
        pb_facts.emit_recovered(
            sink,
            run_id,
            state,
            yes_book,
            no_book,
            recovery_source=recovery_source,
            recovery_action=recovery_action,
        )

    result = save_persisted_state(path, state)
    if not result.success and sink and run_id:
        pb_facts.emit_state_persist_failed(sink, run_id, state, result)
    return state
