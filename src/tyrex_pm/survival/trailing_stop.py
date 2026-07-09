"""Survivor trailing stop FSM using executable evidence (Phase 1 simplified)."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from tyrex_pm.runtime.config import TrailingStopConfig
from tyrex_pm.survival.enforcement import is_enforce
from tyrex_pm.survival.exit_planning import SurvivalExitPlanner
from tyrex_pm.survival.models import (
    SurvivalExitEvaluation,
    TrailingStopEvaluation,
    TrailingStopRuntime,
    TrailingStopState,
)


class SurvivorTrailingStop:
    def evaluate(
        self,
        *,
        runtime: TrailingStopRuntime,
        exit_eval: SurvivalExitEvaluation,
        survivor_entry: Decimal,
        survivor_bid_0: Decimal | None,
        loser_exit_ts: float,
        timing: Any | None = None,
        cfg: TrailingStopConfig,
        planner: SurvivalExitPlanner | None = None,
        breakeven_price: Decimal | None = None,
    ) -> TrailingStopEvaluation:
        if not cfg.enabled:
            return TrailingStopEvaluation(
                new_runtime=runtime,
                should_exit=False,
                reason=None,
                evidence={"enabled": False},
            )

        ev = exit_eval.evidence
        bid = self._progress_bid(exit_eval, planner)
        now = time.time()
        seconds_to_close = getattr(timing, "seconds_to_close", None) if timing else None

        if seconds_to_close is not None and seconds_to_close <= cfg.disable_near_close_s:
            return TrailingStopEvaluation(
                new_runtime=runtime,
                should_exit=False,
                reason="near_close_disabled",
                evidence={"seconds_to_close": seconds_to_close},
            )

        new_rt = TrailingStopRuntime(
            state=runtime.state,
            peak_executable_bid=runtime.peak_executable_bid,
            trail_floor=runtime.trail_floor,
            armed_at_ts=runtime.armed_at_ts,
        )
        armed_transition = False
        triggered_transition = False
        should_exit = False
        reason: str | None = None

        if bid is None:
            return TrailingStopEvaluation(
                new_runtime=new_rt,
                should_exit=False,
                reason="no_executable_bid",
                evidence={"quality_verdict": ev.quality_verdict},
            )

        ref_bid = survivor_bid_0 if survivor_bid_0 is not None else survivor_entry
        elapsed = now - loser_exit_ts
        gain = bid - ref_bid

        liquidity_ok = (
            ev.available_depth_fraction >= cfg.min_depth_fraction
            and (ev.spread is None or ev.spread <= cfg.max_spread)
        )
        if cfg.require_fresh_book:
            if cfg.max_book_age_s is not None and ev.book_age_ms is not None:
                liquidity_ok = liquidity_ok and ev.book_age_ms <= int(cfg.max_book_age_s * 1000)
            elif ev.quality_verdict in {"reject_decision"}:
                liquidity_ok = False

        activation_mode = cfg.activation_mode
        recovery_threshold = None
        if breakeven_price is not None:
            recovery_threshold = breakeven_price + cfg.recovery_buffer

        can_arm = elapsed >= cfg.arm_delay_s and liquidity_ok
        if activation_mode == "immediate":
            pass
        elif activation_mode == "loss_recovered":
            can_arm = can_arm and recovery_threshold is not None and bid >= recovery_threshold
        else:
            can_arm = can_arm and gain >= cfg.arm_after_executable_gain

        if new_rt.state == TrailingStopState.DISARMED and can_arm:
            new_rt.state = TrailingStopState.ARMED
            new_rt.peak_executable_bid = bid
            new_rt.trail_floor = bid - cfg.trail_distance
            min_lock = survivor_entry + cfg.min_profit_lock
            if new_rt.trail_floor < min_lock:
                new_rt.trail_floor = min_lock
            new_rt.armed_at_ts = now
            armed_transition = True

        if new_rt.state == TrailingStopState.ARMED and bid is not None:
            if new_rt.peak_executable_bid is None or bid > new_rt.peak_executable_bid:
                new_rt.peak_executable_bid = bid
                new_rt.trail_floor = bid - cfg.trail_distance
                min_lock = survivor_entry + cfg.min_profit_lock
                if new_rt.trail_floor is not None and new_rt.trail_floor < min_lock:
                    new_rt.trail_floor = min_lock

            floor = new_rt.trail_floor
            if floor is not None and bid <= floor:
                new_rt.state = TrailingStopState.TRIGGERED
                triggered_transition = True
                should_exit = True
                reason = "trail_floor_breach"

        if should_exit and not is_enforce(cfg.enforcement_mode):
            should_exit = False
            reason = "advisory_only"

        evidence: dict[str, Any] = {
            "executable_bid": str(bid),
            "touch_bid": str(ev.touch_bid) if ev.touch_bid is not None else None,
            "peak_executable_bid": str(new_rt.peak_executable_bid) if new_rt.peak_executable_bid else None,
            "trail_floor": str(new_rt.trail_floor) if new_rt.trail_floor else None,
            "spread": str(ev.spread) if ev.spread is not None else None,
            "depth_fraction": str(ev.available_depth_fraction),
            "enforcement_mode": cfg.enforcement_mode,
            "state": new_rt.state.value,
            "activation_mode": activation_mode,
            "breakeven_price": str(breakeven_price) if breakeven_price is not None else None,
            "recovery_buffer": str(cfg.recovery_buffer),
            "recovery_threshold": str(recovery_threshold) if recovery_threshold is not None else None,
        }
        return TrailingStopEvaluation(
            new_runtime=new_rt,
            should_exit=should_exit,
            reason=reason,
            evidence=evidence,
            armed_transition=armed_transition,
            triggered_transition=triggered_transition,
        )

    @staticmethod
    def _progress_bid(
        exit_eval: SurvivalExitEvaluation,
        planner: SurvivalExitPlanner | None,
    ) -> Decimal | None:
        if planner is not None:
            bid = planner.executable_bid_for_progress(exit_eval)
            if bid is not None:
                return bid
        ev = exit_eval.evidence
        return ev.sweep_vwap or ev.executable_bid or ev.touch_bid
