"""Survivor stall / no-progress detection (Phase 1 M3)."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from tyrex_pm.runtime.config import StallExitConfig
from tyrex_pm.survival.exit_planning import SurvivalExitPlanner
from tyrex_pm.survival.models import StallAction, StallEvaluation, SurvivorLegState, SurvivalExitEvaluation

_EPS = Decimal("0.0001")


class SurvivorStallDetector:
    def __init__(self, cfg: StallExitConfig) -> None:
        self._cfg = cfg

    def record_baseline(
        self,
        *,
        exit_eval: SurvivalExitEvaluation,
        selected_target: Decimal,
        loser_exit_ts: float,
        seconds_to_close: float | None,
        flatten_before_event_end_s: float,
        selected_mode: str = "full_recovery",
        planner: SurvivalExitPlanner | None = None,
    ) -> SurvivorLegState:
        bid = self._executable_bid(exit_eval, planner)
        available = None
        if seconds_to_close is not None:
            available = max(seconds_to_close - flatten_before_event_end_s, float(_EPS))
        from tyrex_pm.survival.models import SurvivorTargetMode

        try:
            mode = SurvivorTargetMode(selected_mode)
        except ValueError:
            mode = SurvivorTargetMode.FULL_RECOVERY
        return SurvivorLegState(
            survivor_bid_0=bid,
            selected_target=selected_target,
            selected_mode=mode,
            loser_exit_ts=loser_exit_ts,
            seconds_to_close_0=seconds_to_close,
            available_survival_time=available,
        )

    def evaluate(
        self,
        *,
        state: SurvivorLegState,
        exit_eval: SurvivalExitEvaluation,
        now_ts: float | None = None,
        planner: SurvivalExitPlanner | None = None,
    ) -> StallEvaluation:
        now = now_ts if now_ts is not None else time.time()
        bid = self._executable_bid(exit_eval, planner)
        bid_0 = state.survivor_bid_0 or Decimal("0")
        target_span = max(state.selected_target - bid_0, _EPS)
        progress = Decimal("0")
        if bid is not None:
            progress = (bid - bid_0) / target_span
            if progress < 0:
                progress = Decimal("0")

        elapsed = max(now - state.loser_exit_ts, 0.0)
        avail = max(float(state.available_survival_time or 1.0), float(_EPS))
        elapsed_fraction = Decimal(str(elapsed / avail))

        stalled = False
        action: StallAction | None = None

        cond_a = (
            elapsed_fraction >= self._cfg.stall_threshold_fraction
            and progress < self._cfg.min_progress_ratio
            and elapsed >= self._cfg.check_after_s
        )
        cond_b = elapsed >= self._cfg.max_stall_s and progress < self._cfg.min_progress_ratio
        if self._cfg.enabled and (cond_a or cond_b):
            stalled = True
            raw_action = self._cfg.action_when_stalled.lower()
            if raw_action == "exit_small_loss":
                action = StallAction.EXIT_SMALL_LOSS
            elif raw_action == "exit_market":
                action = StallAction.EXIT_MARKET
            else:
                action = StallAction.DOWNGRADE

        evidence: dict[str, Any] = {
            "progress_to_target": str(progress),
            "elapsed_fraction": str(elapsed_fraction),
            "elapsed_since_loser_exit": elapsed,
            "executable_bid": str(bid) if bid is not None else None,
            "survivor_bid_0": str(bid_0),
            "selected_target": str(state.selected_target),
            "enforcement_mode": self._cfg.enforcement_mode,
            "stall_condition_a": cond_a,
            "stall_condition_b": cond_b,
        }
        return StallEvaluation(
            stalled=stalled,
            progress_to_target=progress,
            elapsed_fraction=elapsed_fraction,
            action=action,
            evidence=evidence,
        )

    @staticmethod
    def _executable_bid(
        exit_eval: SurvivalExitEvaluation,
        planner: SurvivalExitPlanner | None,
    ) -> Decimal | None:
        if planner is not None:
            bid = planner.executable_bid_for_progress(exit_eval)
            if bid is not None:
                return bid
        ev = exit_eval.evidence
        return ev.sweep_vwap or ev.executable_bid
