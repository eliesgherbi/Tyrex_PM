"""Paired binary pair-level percentage PnL tests (Phase 4.6)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.errors import ConfigError
from tyrex_pm.core.enums import OrderStyle
from tyrex_pm.core.ids import TokenId
from tyrex_pm.runtime.config import PairedBinaryStrategyConfig, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore, make_snapshot
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.entry_eval import EntryEvalInput, LegBook, evaluate_entry
from tyrex_pm.strategies.paired_binary.exit_engine import (
    check_activation_loss_budget,
    ensure_pnl_budgets,
    evaluate_dual_stop,
    reprice_survivor_after_loser_exit,
)
from tyrex_pm.strategies.paired_binary.pnl import (
    compute_pair_pnl_budgets,
    desired_net_profit_per_pair,
    price_based_pnl_estimate_from_stored_prices,
    reprice_survivor_target_after_loser_exit,
    static_survivor_target,
    trigger_stop_price,
    trigger_target_price,
)
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"


def _cfg(**over) -> PairedBinaryStrategyConfig:
    base = dict(
        enabled=True,
        owner_id="paired_binary",
        market_id="m1",
        yes_token_id=YES,
        no_token_id=NO,
        position_size=Decimal("5"),
        max_pair_entry_cost=Decimal("1.02"),
        max_spread_yes=Decimal("0.02"),
        max_spread_no=Decimal("0.02"),
        pair_stop_loss_pct=Decimal("0.02"),
        pair_take_profit_pct=Decimal("0.05"),
        slippage_buffer=Decimal("0.005"),
        reject_if_spread_exceeds_loss_budget=True,
        max_holding_time_s=3600.0,
        entry_order_style=OrderStyle.GTC,
        exit_order_style=OrderStyle.FAK,
        entry_fill_timeout_s=60.0,
        abort_unpaired_entry=True,
        unwind_partial_entry=True,
        min_effective_pair_qty=Decimal("5"),
        run_once=True,
        max_markets=1,
        tick_interval_s=0.01,
        max_book_age_s=5.0,
    )
    base.update(over)
    return PairedBinaryStrategyConfig(**base)


def _leg(tid: str, bid: str, ask: str) -> LegBook:
    return LegBook(TokenId(tid), Decimal(bid), Decimal(ask), False)


def _state(yes_entry: str = "0.49", no_entry: str = "0.51") -> PairedBinaryRuntimeState:
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.BOTH_LEGS_ACTIVE,
        yes_entry=Decimal(yes_entry),
        no_entry=Decimal(no_entry),
        effective_qty=Decimal("5"),
    )
    ensure_pnl_budgets(state, _cfg())
    return state


def test_config_rejects_old_loser_stop_loss() -> None:
    with pytest.raises(ConfigError, match="pair_stop_loss_pct"):
        parse_app_config(
            risk={"notional": {"min_usd": "1", "max_usd": "100", "max_policy": "cap"}},
            runtime={"execution_mode": "shadow"},
            strategy={
                "kind": "paired_binary",
                "paired_binary": {
                    "market_id": "m",
                    "yes_token_id": YES,
                    "no_token_id": NO,
                    "loser_stop_loss": "0.02",
                },
            },
        )


def test_config_rejects_old_winner_take_profit() -> None:
    with pytest.raises(ConfigError, match="pair_take_profit_pct"):
        parse_app_config(
            risk={"notional": {"min_usd": "1", "max_usd": "100", "max_policy": "cap"}},
            runtime={"execution_mode": "shadow"},
            strategy={
                "kind": "paired_binary",
                "paired_binary": {
                    "market_id": "m",
                    "yes_token_id": YES,
                    "no_token_id": NO,
                    "winner_take_profit": "0.05",
                },
            },
        )


def test_pair_pnl_pct_budgets_from_pair_cost() -> None:
    budgets = compute_pair_pnl_budgets(
        Decimal("0.48"),
        Decimal("0.52"),
        pair_stop_loss_pct=Decimal("0.02"),
        pair_take_profit_pct=Decimal("0.05"),
        slippage_buffer=Decimal("0.005"),
    )
    assert budgets.pair_cost == Decimal("1.00")
    assert budgets.loss_budget == Decimal("0.02")
    assert budgets.profit_budget == Decimal("0.05")


def test_yes_loser_stop_price_from_pair_loss_budget() -> None:
    budgets = compute_pair_pnl_budgets(
        Decimal("0.48"),
        Decimal("0.52"),
        pair_stop_loss_pct=Decimal("0.02"),
        pair_take_profit_pct=Decimal("0.05"),
        slippage_buffer=Decimal("0.005"),
    )
    assert budgets.yes_planned_stop == Decimal("0.46")
    assert budgets.yes_trigger_stop == Decimal("0.465")


def test_no_loser_stop_price_from_pair_loss_budget() -> None:
    budgets = compute_pair_pnl_budgets(
        Decimal("0.48"),
        Decimal("0.52"),
        pair_stop_loss_pct=Decimal("0.02"),
        pair_take_profit_pct=Decimal("0.05"),
        slippage_buffer=Decimal("0.005"),
    )
    assert budgets.no_planned_stop == Decimal("0.50")
    assert budgets.no_trigger_stop == Decimal("0.505")


def test_survivor_target_from_pair_profit_budget() -> None:
    budgets = compute_pair_pnl_budgets(
        Decimal("0.48"),
        Decimal("0.52"),
        pair_stop_loss_pct=Decimal("0.02"),
        pair_take_profit_pct=Decimal("0.05"),
        slippage_buffer=Decimal("0.005"),
    )
    planned = Decimal("0.52") + budgets.profit_budget
    assert planned == Decimal("0.57")
    assert trigger_target_price(planned, Decimal("0.005")) == Decimal("0.575")


def test_expected_pnl_equals_pair_cost_times_tp_minus_sl() -> None:
    pc = Decimal("1.01")
    net = desired_net_profit_per_pair(
        pc,
        pair_stop_loss_pct=Decimal("0.02"),
        pair_take_profit_pct=Decimal("0.05"),
    )
    assert net == Decimal("1.01") * Decimal("0.03")


def test_entry_rejects_yes_spread_exceeds_loss_budget() -> None:
    yes = _leg(YES, "0.40", "0.49")
    no = _leg(NO, "0.50", "0.51")
    r = evaluate_entry(
        EntryEvalInput(
            yes=yes,
            no=no,
            max_pair_entry_cost=Decimal("1.02"),
            max_spread_yes=Decimal("0.20"),
            max_spread_no=Decimal("0.02"),
            pair_stop_loss_pct=Decimal("0.02"),
            slippage_buffer=Decimal("0.005"),
            reject_if_spread_exceeds_loss_budget=True,
        )
    )
    assert r.allowed is False
    assert r.reason == rc.YES_SPREAD_EXCEEDS_LOSS_BUDGET


def test_entry_rejects_no_spread_exceeds_loss_budget() -> None:
    yes = _leg(YES, "0.48", "0.49")
    no = _leg(NO, "0.40", "0.51")
    r = evaluate_entry(
        EntryEvalInput(
            yes=yes,
            no=no,
            max_pair_entry_cost=Decimal("1.02"),
            max_spread_yes=Decimal("0.02"),
            max_spread_no=Decimal("0.20"),
            pair_stop_loss_pct=Decimal("0.02"),
            slippage_buffer=Decimal("0.005"),
            reject_if_spread_exceeds_loss_budget=True,
        )
    )
    assert r.allowed is False
    assert r.reason == rc.NO_SPREAD_EXCEEDS_LOSS_BUDGET


def test_activation_rejects_immediate_exit_gap_exceeds_loss_budget() -> None:
    cfg = _cfg()
    state = _state()
    yes_book = _leg(YES, "0.40", "0.49")
    no_book = _leg(NO, "0.50", "0.51")
    result = check_activation_loss_budget(state, cfg, yes_book, no_book)
    assert result.ok is False
    assert result.reason == rc.YES_ACTIVATION_GAP_EXCEEDS_LOSS_BUDGET


def test_slippage_buffer_triggers_stop_earlier() -> None:
    cfg = _cfg(pair_stop_loss_pct=Decimal("0.02"), slippage_buffer=Decimal("0.005"))
    state = _state()
    yes_book = _leg(YES, "0.476", "0.49")
    no_book = _leg(NO, "0.50", "0.51")
    assert evaluate_dual_stop(state, cfg, yes_book, no_book) is None
    yes_book = _leg(YES, "0.474", "0.49")
    assert evaluate_dual_stop(state, cfg, yes_book, no_book) == "yes"


def test_slippage_buffer_requires_higher_winner_target() -> None:
    budgets = compute_pair_pnl_budgets(
        Decimal("0.49"),
        Decimal("0.51"),
        pair_stop_loss_pct=Decimal("0.02"),
        pair_take_profit_pct=Decimal("0.05"),
        slippage_buffer=Decimal("0.005"),
    )
    planned = Decimal("0.51") + budgets.profit_budget
    assert trigger_target_price(planned, Decimal("0.005")) > planned


def test_realized_loser_loss_reprices_winner_target() -> None:
    cfg = _cfg()
    state = _state()
    initial = static_survivor_target(
        state.no_entry or Decimal("0.51"),
        state.profit_budget or Decimal("0.05"),
        cfg.slippage_buffer,
    )
    state.no_target = initial.trigger_target
    old, new = reprice_survivor_after_loser_exit(
        state,
        cfg,
        loser_leg="yes",
        loser_exit_fill=Decimal("0.40"),
    )
    assert old == initial.trigger_target
    assert new is not None
    assert new > old


def test_price_based_pnl_estimate_from_stored_prices() -> None:
    result = price_based_pnl_estimate_from_stored_prices(
        yes_entry=Decimal("0.48"),
        no_entry=Decimal("0.52"),
        yes_exit=Decimal("0.46"),
        no_exit=Decimal("0.57"),
        qty=Decimal("5"),
    )
    assert result is not None
    pc, exit_value, pnl_per_pair, pnl_total = result
    assert pc == Decimal("1.00")
    assert exit_value == Decimal("1.03")
    assert pnl_per_pair == Decimal("0.03")
    assert pnl_total == Decimal("0.15")


def test_reprice_survivor_target_math() -> None:
    plan = reprice_survivor_target_after_loser_exit(
        survivor_entry=Decimal("0.52"),
        loser_entry=Decimal("0.48"),
        loser_exit_fill=Decimal("0.40"),
        pair_cost=Decimal("1.00"),
        pair_stop_loss_pct=Decimal("0.02"),
        pair_take_profit_pct=Decimal("0.05"),
        slippage_buffer=Decimal("0.005"),
    )
    assert plan.realized_loser_loss == Decimal("0.08")
    assert plan.required_winner_gain == Decimal("0.11")
    assert plan.planned_target == Decimal("0.63")
    assert plan.trigger_target == Decimal("0.635")
