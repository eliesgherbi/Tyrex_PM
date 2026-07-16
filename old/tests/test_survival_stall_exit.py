"""Unit tests for survivor stall detection (Wave C M3)."""

from __future__ import annotations

import time
from decimal import Decimal

from tyrex_pm.runtime.config import StallExitConfig
from tyrex_pm.survival.models import (
    ExecutableExitEvidence,
    StallAction,
    SurvivalExitEvaluation,
    SurvivorLegState,
    SurvivorTargetMode,
)
from tyrex_pm.survival.stall_exit import SurvivorStallDetector


def _eval_bid(bid: str) -> SurvivalExitEvaluation:
    ev = ExecutableExitEvidence(
        touch_bid=Decimal(bid),
        executable_bid=Decimal(bid),
        sweep_vwap=Decimal(bid),
        worst_price_to_fill=Decimal(bid),
        available_depth=Decimal("10"),
        available_depth_fraction=Decimal("1"),
        expected_slippage=Decimal("0"),
        book_age_ms=100,
        snapshot_id="s1",
        quality_verdict="pass",
        spread=Decimal("0.02"),
        planner_evidence_ref=None,
    )
    return SurvivalExitEvaluation(verdict="proceed_full", recommended_qty=Decimal("5"), evidence=ev, reason=None)


def test_stalled_on_high_elapsed_low_progress() -> None:
    cfg = StallExitConfig(check_after_s=5, max_stall_s=30, stall_threshold_fraction=Decimal("0.4"))
    detector = SurvivorStallDetector(cfg)
    state = SurvivorLegState(
        survivor_bid_0=Decimal("0.50"),
        selected_target=Decimal("0.65"),
        selected_mode=SurvivorTargetMode.FULL_RECOVERY,
        loser_exit_ts=time.time() - 50,
        seconds_to_close_0=100.0,
        available_survival_time=80.0,
    )
    result = detector.evaluate(state=state, exit_eval=_eval_bid("0.51"), now_ts=time.time())
    assert result.stalled
    assert result.progress_to_target < cfg.min_progress_ratio


def test_max_stall_s_triggers() -> None:
    cfg = StallExitConfig(max_stall_s=30, check_after_s=0)
    detector = SurvivorStallDetector(cfg)
    state = SurvivorLegState(
        survivor_bid_0=Decimal("0.50"),
        selected_target=Decimal("0.65"),
        selected_mode=SurvivorTargetMode.FULL_RECOVERY,
        loser_exit_ts=time.time() - 35,
        seconds_to_close_0=100.0,
        available_survival_time=80.0,
    )
    result = detector.evaluate(state=state, exit_eval=_eval_bid("0.505"), now_ts=time.time())
    assert result.stalled


def test_advisory_does_not_clear_recommended_action() -> None:
    cfg = StallExitConfig(enforcement_mode="advisory", max_stall_s=10, check_after_s=0)
    detector = SurvivorStallDetector(cfg)
    state = SurvivorLegState(
        survivor_bid_0=Decimal("0.50"),
        selected_target=Decimal("0.65"),
        selected_mode=SurvivorTargetMode.FULL_RECOVERY,
        loser_exit_ts=time.time() - 20,
        seconds_to_close_0=100.0,
        available_survival_time=80.0,
    )
    result = detector.evaluate(state=state, exit_eval=_eval_bid("0.50"), now_ts=time.time())
    assert result.stalled
    assert result.action == StallAction.DOWNGRADE
