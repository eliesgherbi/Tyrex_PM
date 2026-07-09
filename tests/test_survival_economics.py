"""Unit tests for exit economics gate (Wave C M5)."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.runtime.config import EconomicsConfig
from tyrex_pm.survival.economics import ExitEconomics
from tyrex_pm.survival.models import (
    EconomicsContext,
    EconomicsVerdict,
    ExecutableExitEvidence,
    SurvivalExitEvaluation,
    SurvivorTargetMode,
)


def _exit_eval(price: str) -> SurvivalExitEvaluation:
    ev = ExecutableExitEvidence(
        touch_bid=Decimal(price),
        executable_bid=Decimal(price),
        sweep_vwap=Decimal(price),
        worst_price_to_fill=Decimal(price),
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


def test_expected_net_from_ledger_and_executable() -> None:
    cfg = EconomicsConfig(enforcement_mode="advisory")
    ctx = EconomicsContext(
        phase="survivor_hold",
        total_entry_cost=Decimal("5.0"),
        loser_exit_proceeds=Decimal("2.0"),
        survivor_qty=Decimal("5"),
        exit_eval=_exit_eval("0.55"),
        selected_target_mode=SurvivorTargetMode.BREAKEVEN,
        estimated_fees=Decimal("0"),
        slippage_buffer=Decimal("0.005"),
        minimum_acceptable_net=Decimal("-0.02"),
    )
    result = ExitEconomics().evaluate(ctx, cfg)
    assert result.expected_net_total is not None
    assert result.expected_net_total == Decimal("2.0") + Decimal("0.55") * Decimal("5") - Decimal("0.005") * Decimal("5") - Decimal("5.0")


def test_advisory_emits_exit_verdict_without_enforcement() -> None:
    cfg = EconomicsConfig(
        enforcement_mode="advisory",
        exit_survivor_if_expected_net_below=Decimal("0"),
    )
    ctx = EconomicsContext(
        phase="survivor_hold",
        total_entry_cost=Decimal("5.0"),
        loser_exit_proceeds=Decimal("1.0"),
        survivor_qty=Decimal("5"),
        exit_eval=_exit_eval("0.40"),
        selected_target_mode=SurvivorTargetMode.SMALL_LOSS,
        estimated_fees=Decimal("0"),
        slippage_buffer=Decimal("0"),
        minimum_acceptable_net=Decimal("-0.02"),
    )
    result = ExitEconomics().evaluate(ctx, cfg)
    assert result.verdict == EconomicsVerdict.PROCEED


def test_enforce_can_exit_survivor_early() -> None:
    cfg = EconomicsConfig(
        enforcement_mode="enforce",
        exit_survivor_if_expected_net_below=Decimal("0"),
    )
    ctx = EconomicsContext(
        phase="survivor_hold",
        total_entry_cost=Decimal("5.0"),
        loser_exit_proceeds=Decimal("1.0"),
        survivor_qty=Decimal("5"),
        exit_eval=_exit_eval("0.40"),
        selected_target_mode=SurvivorTargetMode.SMALL_LOSS,
        estimated_fees=Decimal("0"),
        slippage_buffer=Decimal("0"),
        minimum_acceptable_net=Decimal("-0.02"),
    )
    result = ExitEconomics().evaluate(ctx, cfg)
    assert result.verdict == EconomicsVerdict.EXIT_SURVIVOR_EARLY
