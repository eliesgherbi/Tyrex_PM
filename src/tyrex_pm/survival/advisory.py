"""Survivor-phase survival advisory orchestration (Phase 1 simplified)."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Literal

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.market_data.quality import DecisionContext
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import AppConfig, PairedBinaryStrategyConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.strategies.paired_binary import facts as pb_facts
from tyrex_pm.strategies.paired_binary.entry_eval import LegBook
from tyrex_pm.strategies.paired_binary.state import PairedBinaryRuntimeState
from tyrex_pm.survival.enforcement import is_enforce
from tyrex_pm.survival.exit_planning import SurvivalExitPlanner, planner_from_app
from tyrex_pm.survival.models import (
    SurvivalAdvisoryResult,
    TrailingStopRuntime,
    TrailingStopState,
)
from tyrex_pm.survival.recovery_level import breakeven_from_state
from tyrex_pm.survival.survivor_floor import SurvivorFloorEvaluation, SurvivorHardFloor
from tyrex_pm.survival.trailing_stop import SurvivorTrailingStop

SurvivorLeg = Literal["yes", "no"]


def trailing_runtime_from_state(raw: dict[str, Any] | None) -> TrailingStopRuntime:
    if not raw:
        return TrailingStopRuntime()
    ts = raw.get("trailing_stop") or {}
    state_raw = str(ts.get("state", TrailingStopState.DISARMED.value))
    try:
        state = TrailingStopState(state_raw)
    except ValueError:
        state = TrailingStopState.DISARMED
    peak = ts.get("peak_executable_bid")
    floor = ts.get("trail_floor")
    armed = ts.get("armed_at_ts")
    return TrailingStopRuntime(
        state=state,
        peak_executable_bid=Decimal(str(peak)) if peak else None,
        trail_floor=Decimal(str(floor)) if floor else None,
        armed_at_ts=float(armed) if armed is not None else None,
    )


def persist_trailing_runtime(state: PairedBinaryRuntimeState, runtime: TrailingStopRuntime) -> None:
    if state.survivor_leg_state is None:
        state.survivor_leg_state = {}
    state.survivor_leg_state["trailing_stop"] = {
        "state": runtime.state.value,
        "peak_executable_bid": str(runtime.peak_executable_bid) if runtime.peak_executable_bid else None,
        "trail_floor": str(runtime.trail_floor) if runtime.trail_floor else None,
        "armed_at_ts": runtime.armed_at_ts,
    }


def _survivor_leg_and_entry(state: PairedBinaryRuntimeState) -> tuple[SurvivorLeg, Decimal | None]:
    from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase

    if state.phase == PairedBinaryPhase.ONLY_YES_ACTIVE:
        return "yes", state.yes_entry
    if state.phase == PairedBinaryPhase.ONLY_NO_ACTIVE:
        return "no", state.no_entry
    return "yes", None


def _loser_exit_ts(state: PairedBinaryRuntimeState) -> float:
    raw = state.survivor_leg_state or {}
    ts = raw.get("loser_exit_ts")
    return float(ts) if ts is not None else time.time()


def _stored_hard_floor(state: PairedBinaryRuntimeState) -> Decimal | None:
    raw = state.survivor_leg_state or {}
    hf = raw.get("hard_floor_price")
    return Decimal(str(hf)) if hf is not None else None


def evaluate_survival_advisory(
    *,
    app: AppConfig,
    coord: RuntimeCoordinator,
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
    yes_book: LegBook,
    no_book: LegBook,
    sink: JsonlSink | None,
    run_id: RunId | None,
    timing: Any | None = None,
    flatten_before_event_end_s: float = 20.0,
    planner: SurvivalExitPlanner | None = None,
    monitor_trigger: str = "poll",
    evaluation_started: float | None = None,
) -> SurvivalAdvisoryResult:
    """Simplified Phase 1: hard floor → trailing (post-recovery) only."""
    del flatten_before_event_end_s
    survival = app.survival
    if not survival.enabled:
        return SurvivalAdvisoryResult(None, None, None, None, None)

    leg, survivor_entry = _survivor_leg_and_entry(state)
    if survivor_entry is None:
        return SurvivalAdvisoryResult(None, None, None, None, None)

    started = evaluation_started if evaluation_started is not None else time.time()
    token_id = TokenId(cfg.yes_token_id if leg == "yes" else cfg.no_token_id)
    exit_planner = planner or planner_from_app(app)
    exit_eval = exit_planner.evaluate_exit(
        coord=coord,
        token_id=token_id,
        qty=state.effective_qty,
        decision_context=DecisionContext.TAKE_PROFIT,
    )

    breakeven = breakeven_from_state(state)
    stored_floor = _stored_hard_floor(state)
    survivor_bid_0_raw = (state.survivor_leg_state or {}).get("survivor_bid_0")
    survivor_bid_0 = Decimal(str(survivor_bid_0_raw)) if survivor_bid_0_raw else None

    floor_eval: SurvivorFloorEvaluation = SurvivorHardFloor().evaluate(
        cfg=survival.survivor_floor,
        exit_eval=exit_eval,
        survivor_entry=survivor_entry,
        stored_floor=stored_floor,
        planner=exit_planner,
    )
    if stored_floor is None and survival.survivor_floor.enabled:
        if state.survivor_leg_state is None:
            state.survivor_leg_state = {}
        state.survivor_leg_state["hard_floor_price"] = str(floor_eval.floor_price)

    trailing_rt = trailing_runtime_from_state(state.survivor_leg_state)
    trailing_eval = SurvivorTrailingStop().evaluate(
        runtime=trailing_rt,
        exit_eval=exit_eval,
        survivor_entry=survivor_entry,
        survivor_bid_0=survivor_bid_0,
        loser_exit_ts=_loser_exit_ts(state),
        timing=timing,
        cfg=survival.trailing_stop,
        planner=exit_planner,
        breakeven_price=breakeven,
    )
    persist_trailing_runtime(state, trailing_eval.new_runtime)

    latency_ms = int((time.time() - started) * 1000)
    book_age = exit_eval.evidence.book_age_ms

    if sink and run_id:
        pb_facts.emit_simplified_survival_facts(
            sink,
            run_id,
            state,
            yes_book,
            no_book,
            survivor_leg=leg,
            survival=survival,
            exit_eval=exit_eval,
            floor=floor_eval,
            trailing=trailing_eval,
            breakeven=breakeven,
            requested_qty=state.effective_qty,
            monitor_mode=survival.monitor_mode,
            monitor_trigger=monitor_trigger,
            evaluation_latency_ms=latency_ms,
            book_age_ms=book_age,
        )

    enforce_exit = False
    enforce_reason: str | None = None
    enforce_module: str | None = None

    if floor_eval.should_exit and is_enforce(survival.survivor_floor.enforcement_mode):
        enforce_exit = True
        enforce_reason = floor_eval.reason or "hard_floor_breach"
        enforce_module = "survivor_floor"
    elif trailing_eval.should_exit and is_enforce(survival.trailing_stop.enforcement_mode):
        enforce_exit = True
        enforce_reason = trailing_eval.reason
        enforce_module = "trailing_stop"

    return SurvivalAdvisoryResult(
        exit_eval=exit_eval,
        reachability=None,
        stall=None,
        trailing=trailing_eval,
        economics=None,
        floor=floor_eval,
        recovery=breakeven,
        enforce_exit=enforce_exit,
        enforce_exit_reason=enforce_reason,
        enforce_module=enforce_module,
        enforce_stall_downgrade=False,
    )
