"""Unit tests for ledger-based survivor target policy (Wave B M1)."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.runtime.config import SurvivalConfig, SurvivalTargetPolicyConfig
from tyrex_pm.strategies.paired_binary.exit_engine import reprice_survivor_after_loser_exit
from tyrex_pm.strategies.paired_binary.state import PairedBinaryRuntimeState
from tyrex_pm.survival.models import SurvivorLegContext, SurvivorTargetClassification, SurvivorTargetMode
from tyrex_pm.survival.target_policy import SurvivorTargetPolicy


def _ctx(**over) -> SurvivorLegContext:
    base = dict(
        survivor_leg="no",
        yes_entry_qty=Decimal("5"),
        no_entry_qty=Decimal("5"),
        yes_entry_cash=Decimal("2.40"),
        no_entry_cash=Decimal("2.60"),
        loser_exit_qty=Decimal("5"),
        loser_exit_cash=Decimal("2.00"),
        survivor_remaining_qty=Decimal("5"),
        estimated_fees=Decimal("0"),
        slippage_buffer=Decimal("0.005"),
        desired_net_profit_total=Decimal("0.15"),
        seconds_to_close=120.0,
        loser_exit_ts=1.0,
        ledger_source="matched_cash",
    )
    base.update(over)
    return SurvivorLegContext(**base)


def _policy(**over) -> SurvivorTargetPolicy:
    cfg = SurvivalTargetPolicyConfig(
        mode="dynamic",
        small_loss_max_usd_per_pair=Decimal("0.05"),
        small_profit_min_usd_per_pair=Decimal("0.03"),
        max_reasonable_exit_price=Decimal("0.98"),
        slippage_buffer=None,
        estimated_fee_bps=Decimal("0"),
    )
    if over:
        cfg = SurvivalTargetPolicyConfig(**{**cfg.__dict__, **over})
    return SurvivorTargetPolicy(cfg)


def test_ledger_math_golden_case() -> None:
    ctx = _ctx()
    policy = _policy(mode="full_recovery")
    plan = policy.plan_for_mode(ctx, SurvivorTargetMode.FULL_RECOVERY)
    assert plan.required_survivor_exit_price == Decimal("0.63")
    assert plan.trigger_target == Decimal("0.635")
    assert plan.classification == SurvivorTargetClassification.VALID_CANDIDATE


def test_required_price_above_one_is_impossible() -> None:
    ctx = _ctx(loser_exit_cash=Decimal("0"))
    policy = _policy(mode="full_recovery")
    plan = policy.plan_for_mode(ctx, SurvivorTargetMode.FULL_RECOVERY)
    assert plan.required_survivor_exit_price > Decimal("1")
    assert plan.classification == SurvivorTargetClassification.IMPOSSIBLE


def test_required_price_above_max_reasonable_is_unrealistic() -> None:
    ctx = _ctx(loser_exit_cash=Decimal("0.20"))
    policy = _policy(mode="full_recovery", max_reasonable_exit_price=Decimal("0.98"))
    plan = policy.plan_for_mode(ctx, SurvivorTargetMode.FULL_RECOVERY)
    assert plan.classification == SurvivorTargetClassification.UNREALISTIC
    assert plan.required_survivor_exit_price > Decimal("0.98")


def test_dynamic_downgrades_when_full_recovery_impossible() -> None:
    ctx = _ctx(loser_exit_cash=Decimal("0.20"))
    result = _policy(mode="dynamic").select_plan(ctx)
    assert result.plan.mode != SurvivorTargetMode.FULL_RECOVERY
    assert result.plan.classification == SurvivorTargetClassification.VALID_CANDIDATE
    assert len(result.downgrades) >= 1


def test_small_profit_and_breakeven_formulas() -> None:
    ctx = _ctx()
    policy = _policy()
    breakeven = policy.plan_for_mode(ctx, SurvivorTargetMode.BREAKEVEN)
    assert breakeven.target_total_net == Decimal("0")
    assert breakeven.required_survivor_exit_price == Decimal("0.60")

    small_profit = policy.plan_for_mode(ctx, SurvivorTargetMode.SMALL_PROFIT)
    assert small_profit.target_total_net == Decimal("0.15")
    assert small_profit.required_survivor_exit_price == Decimal("0.63")

    small_loss = policy.plan_for_mode(ctx, SurvivorTargetMode.SMALL_LOSS)
    assert small_loss.target_total_net == Decimal("-0.25")
    assert small_loss.required_survivor_exit_price == Decimal("0.55")


def test_slippage_buffer_applied_to_trigger_only() -> None:
    ctx = _ctx(slippage_buffer=Decimal("0.01"))
    plan = _policy().plan_for_mode(ctx, SurvivorTargetMode.BREAKEVEN)
    assert plan.required_survivor_exit_price == Decimal("0.60")
    assert plan.trigger_target == Decimal("0.61")


def test_survival_disabled_keeps_legacy_repricing() -> None:
    from tyrex_pm.core.enums import OrderStyle
    from tyrex_pm.runtime.config import PairedBinaryStrategyConfig
    from tyrex_pm.strategies.paired_binary.pnl import apply_budgets_to_state, budgets_from_cfg

    cfg = PairedBinaryStrategyConfig(
        enabled=True,
        owner_id="paired_binary",
        market_id="m1",
        yes_token_id="y",
        no_token_id="n",
        position_size=Decimal("5"),
        max_pair_entry_cost=Decimal("1.02"),
        max_spread_yes=Decimal("0.05"),
        max_spread_no=Decimal("0.05"),
        pair_stop_loss_pct=Decimal("0.04"),
        pair_take_profit_pct=Decimal("0.10"),
        slippage_buffer=Decimal("0.005"),
        reject_if_spread_exceeds_loss_budget=False,
        max_holding_time_s=3600,
        use_fixture_book=True,
        tick_interval_s=0.005,
        max_runtime_s=0.01,
        exit_order_style=OrderStyle.FAK,
        entry_order_style=OrderStyle.GTC,
        entry_fill_timeout_s=30.0,
        abort_unpaired_entry=True,
        unwind_partial_entry=True,
        min_effective_pair_qty=Decimal("0"),
        run_once=False,
        max_markets=1,
        max_book_age_s=5.0,
    )
    state = PairedBinaryRuntimeState(
        yes_entry=Decimal("0.48"),
        no_entry=Decimal("0.52"),
        effective_qty=Decimal("5"),
        pair_cost=Decimal("1.00"),
    )
    budgets = budgets_from_cfg(state, cfg)
    assert budgets is not None
    apply_budgets_to_state(state, budgets)

    survival_off = SurvivalConfig(enabled=False)
    outcome_disabled = reprice_survivor_after_loser_exit(
        state,
        cfg,
        loser_leg="yes",
        loser_exit_fill=Decimal("0.40"),
        survival=survival_off,
    )
    state2 = PairedBinaryRuntimeState(
        yes_entry=Decimal("0.48"),
        no_entry=Decimal("0.52"),
        effective_qty=Decimal("5"),
        pair_cost=Decimal("1.00"),
    )
    apply_budgets_to_state(state2, budgets)
    outcome_legacy = reprice_survivor_after_loser_exit(
        state2,
        cfg,
        loser_leg="yes",
        loser_exit_fill=Decimal("0.40"),
    )
    assert outcome_disabled.new_target == outcome_legacy.new_target
    assert outcome_disabled.survival_result is None
    assert outcome_legacy.new_target is not None
    assert outcome_legacy.new_target > Decimal("0.55")
