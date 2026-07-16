"""Survivor hard floor unit tests (Phase 1 simplified)."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.runtime.config import SurvivorFloorConfig
from tyrex_pm.survival.models import ExecutableExitEvidence, SurvivalExitEvaluation
from tyrex_pm.survival.survivor_floor import SurvivorHardFloor


def _eval(*, bid: str, depth: str = "1", spread: str = "0.02") -> SurvivalExitEvaluation:
    ev = ExecutableExitEvidence(
        touch_bid=Decimal(bid),
        executable_bid=Decimal(bid),
        sweep_vwap=Decimal(bid),
        worst_price_to_fill=Decimal(bid),
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


def test_floor_equals_winner_entry_price() -> None:
    floor = SurvivorHardFloor().compute_floor_price(
        survivor_entry=Decimal("0.51"),
        cfg=SurvivorFloorConfig(mode="winner_entry_price", floor_buffer=Decimal("0")),
    )
    assert floor == Decimal("0.51")


def test_hard_floor_trigger_advisory_only() -> None:
    cfg = SurvivorFloorConfig(enforcement_mode="advisory")
    result = SurvivorHardFloor().evaluate(
        cfg=cfg,
        exit_eval=_eval(bid="0.51"),
        survivor_entry=Decimal("0.51"),
        stored_floor=Decimal("0.51"),
        planner=None,
    )
    assert result.reason == "advisory_only"
    assert result.should_exit is False


def test_hard_floor_trigger_enforce() -> None:
    cfg = SurvivorFloorConfig(enforcement_mode="enforce")
    result = SurvivorHardFloor().evaluate(
        cfg=cfg,
        exit_eval=_eval(bid="0.50"),
        survivor_entry=Decimal("0.51"),
        stored_floor=Decimal("0.51"),
        planner=None,
    )
    assert result.should_exit is True
    assert result.reason == "hard_floor_breach"
