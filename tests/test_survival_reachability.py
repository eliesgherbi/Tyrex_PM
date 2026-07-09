"""Unit tests for target reachability scoring (Wave C M3)."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.runtime.config import ReachabilityConfig
from tyrex_pm.survival.models import (
    ExecutableExitEvidence,
    ReachabilityVerdict,
    SurvivalExitEvaluation,
    SurvivorLegState,
    SurvivorTargetMode,
)
from tyrex_pm.survival.reachability import TargetReachabilityScorer


def _exit_eval(*, touch: str, exec_bid: str, depth: str = "1", spread: str = "0.02") -> SurvivalExitEvaluation:
    ev = ExecutableExitEvidence(
        touch_bid=Decimal(touch),
        executable_bid=Decimal(exec_bid),
        sweep_vwap=Decimal(exec_bid),
        worst_price_to_fill=Decimal(exec_bid),
        available_depth=Decimal("10"),
        available_depth_fraction=Decimal(depth),
        expected_slippage=Decimal("0"),
        book_age_ms=100,
        snapshot_id="s1",
        quality_verdict="pass",
        spread=Decimal(spread),
        planner_evidence_ref=None,
    )
    return SurvivalExitEvaluation(verdict="proceed_full", recommended_qty=Decimal("5"), evidence=ev, reason=None)


def _ctx(*, bid_0: str = "0.50", target: str = "0.65") -> SurvivorLegState:
    return SurvivorLegState(
        survivor_bid_0=Decimal(bid_0),
        selected_target=Decimal(target),
        selected_mode=SurvivorTargetMode.FULL_RECOVERY,
        loser_exit_ts=1.0,
        seconds_to_close_0=120.0,
        available_survival_time=100.0,
    )


def test_reachable_when_close_to_target_on_executable_bid() -> None:
    scorer = TargetReachabilityScorer(ReachabilityConfig())
    result = scorer.score(
        ctx=_ctx(),
        target_price=Decimal("0.65"),
        exit_eval=_exit_eval(touch="0.64", exec_bid="0.64"),
    )
    assert result.verdict == ReachabilityVerdict.REACHABLE
    assert result.evidence["executable_bid"] == "0.64"


def test_unlikely_when_far_from_target() -> None:
    scorer = TargetReachabilityScorer(ReachabilityConfig(reachable_min_score=Decimal("0.75")))
    timing = type("T", (), {"seconds_to_close": 10.0, "phase": "active"})()
    result = scorer.score(
        ctx=_ctx(),
        target_price=Decimal("0.65"),
        exit_eval=_exit_eval(touch="0.52", exec_bid="0.52"),
        timing=timing,
    )
    assert result.verdict in {ReachabilityVerdict.UNLIKELY, ReachabilityVerdict.WEAK}


def test_uses_executable_not_touch_when_different() -> None:
    scorer = TargetReachabilityScorer(ReachabilityConfig())
    close = scorer.score(
        ctx=_ctx(),
        target_price=Decimal("0.65"),
        exit_eval=_exit_eval(touch="0.64", exec_bid="0.64"),
    )
    far_touch = scorer.score(
        ctx=_ctx(),
        target_price=Decimal("0.65"),
        exit_eval=_exit_eval(touch="0.70", exec_bid="0.52"),
    )
    assert close.score > far_touch.score
