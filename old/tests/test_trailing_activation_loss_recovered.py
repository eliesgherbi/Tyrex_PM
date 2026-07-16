"""Trailing activation_mode=loss_recovered tests."""

from __future__ import annotations

import time
from decimal import Decimal

from tyrex_pm.runtime.config import TrailingStopConfig
from tyrex_pm.survival.models import (
    ExecutableExitEvidence,
    SurvivalExitEvaluation,
    TrailingStopRuntime,
    TrailingStopState,
)
from tyrex_pm.survival.trailing_stop import SurvivorTrailingStop


def _eval(bid: str) -> SurvivalExitEvaluation:
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


def test_does_not_arm_before_breakeven() -> None:
    cfg = TrailingStopConfig(activation_mode="loss_recovered", arm_delay_s=0)
    result = SurvivorTrailingStop().evaluate(
        runtime=TrailingStopRuntime(),
        exit_eval=_eval("0.52"),
        survivor_entry=Decimal("0.48"),
        survivor_bid_0=Decimal("0.50"),
        loser_exit_ts=time.time() - 10,
        timing=None,
        cfg=cfg,
        breakeven_price=Decimal("0.55"),
    )
    assert result.new_runtime.state == TrailingStopState.DISARMED


def test_arms_after_breakeven() -> None:
    cfg = TrailingStopConfig(
        activation_mode="loss_recovered",
        arm_delay_s=0,
        trail_distance=Decimal("0.02"),
    )
    result = SurvivorTrailingStop().evaluate(
        runtime=TrailingStopRuntime(),
        exit_eval=_eval("0.56"),
        survivor_entry=Decimal("0.48"),
        survivor_bid_0=Decimal("0.50"),
        loser_exit_ts=time.time() - 10,
        timing=None,
        cfg=cfg,
        breakeven_price=Decimal("0.55"),
    )
    assert result.new_runtime.state == TrailingStopState.ARMED
    assert result.evidence["activation_mode"] == "loss_recovered"
    assert result.evidence["breakeven_price"] == "0.55"
