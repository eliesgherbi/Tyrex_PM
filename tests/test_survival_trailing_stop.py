"""Unit tests for survivor trailing stop (Wave C M4)."""

from __future__ import annotations

import time
from decimal import Decimal
from types import SimpleNamespace

from tyrex_pm.runtime.config import TrailingStopConfig
from tyrex_pm.survival.models import (
    ExecutableExitEvidence,
    SurvivalExitEvaluation,
    TrailingStopRuntime,
    TrailingStopState,
)
from tyrex_pm.survival.trailing_stop import SurvivorTrailingStop


def _eval(
    *,
    bid: str,
    touch: str | None = None,
    depth: str = "1",
    spread: str = "0.02",
    age_ms: int = 100,
) -> SurvivalExitEvaluation:
    t = touch or bid
    ev = ExecutableExitEvidence(
        touch_bid=Decimal(t),
        executable_bid=Decimal(bid),
        sweep_vwap=Decimal(bid),
        worst_price_to_fill=Decimal(bid),
        available_depth=Decimal("10"),
        available_depth_fraction=Decimal(depth),
        expected_slippage=Decimal("0"),
        book_age_ms=age_ms,
        snapshot_id="s1",
        quality_verdict="pass",
        spread=Decimal(spread),
        planner_evidence_ref=None,
    )
    return SurvivalExitEvaluation(verdict="proceed_full", recommended_qty=Decimal("5"), evidence=ev, reason=None)


def test_does_not_arm_on_touch_only_thin_depth() -> None:
    cfg = TrailingStopConfig(arm_delay_s=0, activation_mode="executable_gain")
    rt = TrailingStopRuntime()
    ev = _eval(bid="0.55", touch="0.70", depth="0.2")
    result = SurvivorTrailingStop().evaluate(
        runtime=rt,
        exit_eval=ev,
        survivor_entry=Decimal("0.50"),
        survivor_bid_0=Decimal("0.50"),
        loser_exit_ts=time.time() - 10,
        timing=None,
        cfg=cfg,
    )
    assert result.new_runtime.state == TrailingStopState.DISARMED


def test_does_not_arm_on_stale_wide_book() -> None:
    cfg = TrailingStopConfig(
        arm_delay_s=0,
        activation_mode="executable_gain",
        max_book_age_s=1.0,
        max_spread=Decimal("0.03"),
    )
    result = SurvivorTrailingStop().evaluate(
        runtime=TrailingStopRuntime(),
        exit_eval=_eval(bid="0.55", spread="0.10", age_ms=5000),
        survivor_entry=Decimal("0.50"),
        survivor_bid_0=Decimal("0.50"),
        loser_exit_ts=time.time() - 10,
        timing=None,
        cfg=cfg,
    )
    assert result.new_runtime.state == TrailingStopState.DISARMED


def test_arms_and_triggers_on_executable_reversal() -> None:
    cfg = TrailingStopConfig(
        arm_delay_s=0,
        activation_mode="executable_gain",
        trail_distance=Decimal("0.02"),
        enforcement_mode="enforce",
    )
    rt = TrailingStopRuntime()
    arm = SurvivorTrailingStop().evaluate(
        runtime=rt,
        exit_eval=_eval(bid="0.55"),
        survivor_entry=Decimal("0.50"),
        survivor_bid_0=Decimal("0.50"),
        loser_exit_ts=time.time() - 10,
        timing=SimpleNamespace(seconds_to_close=120.0),
        cfg=cfg,
    )
    assert arm.new_runtime.state == TrailingStopState.ARMED
    trigger = SurvivorTrailingStop().evaluate(
        runtime=arm.new_runtime,
        exit_eval=_eval(bid="0.52"),
        survivor_entry=Decimal("0.50"),
        survivor_bid_0=Decimal("0.50"),
        loser_exit_ts=time.time() - 10,
        timing=SimpleNamespace(seconds_to_close=120.0),
        cfg=cfg,
    )
    assert trigger.new_runtime.state == TrailingStopState.TRIGGERED
    assert trigger.should_exit is True


def test_advisory_does_not_request_exit() -> None:
    cfg = TrailingStopConfig(
        enforcement_mode="advisory",
        activation_mode="executable_gain",
        arm_delay_s=0,
        trail_distance=Decimal("0.01"),
    )
    arm = SurvivorTrailingStop().evaluate(
        runtime=TrailingStopRuntime(),
        exit_eval=_eval(bid="0.56"),
        survivor_entry=Decimal("0.50"),
        survivor_bid_0=Decimal("0.50"),
        loser_exit_ts=time.time() - 10,
        timing=SimpleNamespace(seconds_to_close=120.0),
        cfg=cfg,
    )
    trigger = SurvivorTrailingStop().evaluate(
        runtime=arm.new_runtime,
        exit_eval=_eval(bid="0.52"),
        survivor_entry=Decimal("0.50"),
        survivor_bid_0=Decimal("0.50"),
        loser_exit_ts=time.time() - 10,
        timing=SimpleNamespace(seconds_to_close=120.0),
        cfg=cfg,
    )
    assert trigger.should_exit is False


def test_near_close_disables_trailing() -> None:
    cfg = TrailingStopConfig(
        disable_near_close_s=30,
        arm_delay_s=0,
        activation_mode="executable_gain",
    )
    result = SurvivorTrailingStop().evaluate(
        runtime=TrailingStopRuntime(),
        exit_eval=_eval(bid="0.56"),
        survivor_entry=Decimal("0.50"),
        survivor_bid_0=Decimal("0.50"),
        loser_exit_ts=time.time() - 10,
        timing=SimpleNamespace(seconds_to_close=20.0),
        cfg=cfg,
    )
    assert result.new_runtime.state == TrailingStopState.DISARMED
    assert result.reason == "near_close_disabled"
