"""Runtime hooks for survival kill switches (Phase 1 M6)."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from tyrex_pm.runtime.paired_binary_shutdown import handle_open_exposure_at_shutdown, phase_has_open_exposure
from tyrex_pm.strategies.paired_binary import facts as pb_facts
from tyrex_pm.strategies.paired_binary.lifecycle import transition_phase
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase
from tyrex_pm.survival.kill_switches import (
    ACTION_FORCE_FLATTEN_PAIR,
    ACTION_HARD_STOP,
    KillSwitchDecision,
    KillSwitchManager,
    manager_from_app,
)

if TYPE_CHECKING:
    from tyrex_pm.runtime.config import AppConfig
    from tyrex_pm.strategies.paired_binary.config import PairedBinaryStrategyConfig
    from tyrex_pm.strategies.paired_binary.state import PairedBinaryRuntimeState


def init_kill_switch_manager(app: AppConfig, *, state_dir) -> KillSwitchManager | None:
    mgr = manager_from_app(app, state_dir=state_dir)
    if mgr is not None:
        mgr.reset_daily_if_needed(time.time())
    return mgr


def emit_kill_switch_fact(
    *,
    sink,
    run_id,
    state,
    yes_book,
    no_book,
    cfg: PairedBinaryStrategyConfig,
    decision: KillSwitchDecision,
) -> None:
    if not decision.triggered:
        return
    pb_facts.emit_kill_switch_triggered(
        sink,
        run_id,
        state,
        yes_book,
        no_book,
        switch_name=decision.switch_name or "unknown",
        threshold=decision.threshold,
        current_value=decision.current_value,
        action=decision.action or "unknown",
        pair_id=cfg.market_id,
        owner_id=cfg.owner_id,
        reason=decision.reason or f"kill_switch_{decision.switch_name}",
    )


async def apply_kill_switch_force_flatten(
    *,
    app: AppConfig,
    run_id,
    coord,
    sink,
    oms,
    strategy,
    cfg: PairedBinaryStrategyConfig,
    state: PairedBinaryRuntimeState,
    yes_book,
    no_book,
    apply_local_shadow_fill: bool,
    live_clob_client,
    unwind_leg_fn,
    decision: KillSwitchDecision,
) -> bool:
    """Force-flatten via existing reduce-only shutdown path. Returns True if loop should stop."""
    if decision.action != ACTION_FORCE_FLATTEN_PAIR:
        return False
    if not phase_has_open_exposure(state.phase):
        return False
    reason = f"kill_switch_{decision.switch_name}"
    emit_kill_switch_fact(
        sink=sink,
        run_id=run_id,
        state=state,
        yes_book=yes_book,
        no_book=no_book,
        cfg=cfg,
        decision=decision,
    )
    await handle_open_exposure_at_shutdown(
        app=app,
        run_id=run_id,
        coord=coord,
        sink=sink,
        oms=oms,
        strategy=strategy,
        cfg=cfg,
        state=state,
        yes_book=yes_book,
        no_book=no_book,
        apply_local_shadow_fill=apply_local_shadow_fill,
        live_clob_client=live_clob_client,
        unwind_leg_fn=unwind_leg_fn,
        shutdown_reason=reason,
    )
    return state.is_terminal()


def apply_kill_switch_hard_stop(
    *,
    state: PairedBinaryRuntimeState,
    decision: KillSwitchDecision,
) -> bool:
    if decision.action != ACTION_HARD_STOP:
        return False
    if not state.is_terminal():
        transition_phase(state, PairedBinaryPhase.FAILED, reason=f"kill_switch_{decision.switch_name}")
    return True


def finalize_kill_switch_counters(
    mgr: KillSwitchManager | None,
    *,
    state: PairedBinaryRuntimeState,
    had_entry: bool,
    pnl: Decimal | None,
    manual_intervention: bool = False,
) -> None:
    if mgr is None:
        return
    if manual_intervention:
        mgr.record_manual_intervention()
    if state.is_terminal():
        mgr.record_lifecycle_terminal(state.phase.value, pnl)
    if not had_entry and state.phase == PairedBinaryPhase.IDLE:
        mgr.record_no_entry_run()


__all__ = [
    "apply_kill_switch_force_flatten",
    "apply_kill_switch_hard_stop",
    "emit_kill_switch_fact",
    "finalize_kill_switch_counters",
    "init_kill_switch_manager",
]
