"""Paired binary state machine and lifecycle tests (Phase 4.6)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.enums import OrderStyle
from tyrex_pm.runtime.config import PairedBinaryStrategyConfig
from tyrex_pm.strategies.paired_binary.lifecycle import (
    activate_both_legs,
    apply_no_stop_loss,
    apply_yes_stop_loss,
    choose_dual_stop_leg,
    compute_effective_qty,
    resolve_min_effective_pair_qty,
)
from tyrex_pm.strategies.paired_binary.pnl import static_survivor_target
from tyrex_pm.strategies.paired_binary.state import (
    PairedBinaryPhase,
    PairedBinaryRuntimeState,
    load_persisted_state,
    persistence_path,
    save_persisted_state,
)


def _cfg() -> PairedBinaryStrategyConfig:
    return PairedBinaryStrategyConfig(
        enabled=True,
        owner_id="paired_binary",
        market_id="m1",
        yes_token_id="y",
        no_token_id="n",
        position_size=Decimal("5"),
        max_pair_entry_cost=Decimal("1.02"),
        max_spread_yes=Decimal("0.02"),
        max_spread_no=Decimal("0.02"),
        pair_stop_loss_pct=Decimal("0.02"),
        pair_take_profit_pct=Decimal("0.10"),
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
        tick_interval_s=0.1,
        max_book_age_s=5.0,
    )


def test_both_legs_active_state_transition() -> None:
    from tyrex_pm.core.ids import TokenId
    from tyrex_pm.strategies.paired_binary.entry_eval import LegBook

    state = PairedBinaryRuntimeState()
    yes_book = LegBook(token_id=TokenId("y"), bid=Decimal("0.48"), ask=Decimal("0.49"), stale=False)
    no_book = LegBook(token_id=TokenId("n"), bid=Decimal("0.50"), ask=Decimal("0.51"), stale=False)
    excess = activate_both_legs(
        state,
        yes_qty=Decimal("5"),
        no_qty=Decimal("5"),
        yes_entry=Decimal("0.49"),
        no_entry=Decimal("0.51"),
        entry_price_source="persisted",
        yes_book=yes_book,
        no_book=no_book,
    )
    assert state.phase == PairedBinaryPhase.BOTH_LEGS_ACTIVE
    assert state.effective_qty == Decimal("5")
    assert excess is None


def test_minimum_effective_qty_threshold() -> None:
    min_q = resolve_min_effective_pair_qty(Decimal("5"), venue_min_size=Decimal("5"))
    assert compute_effective_qty(Decimal("3"), Decimal("3")) < min_q


def test_yes_stop_loss_sets_survivor_target() -> None:
    cfg = _cfg()
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.BOTH_LEGS_ACTIVE,
        yes_entry=Decimal("0.50"),
        no_entry=Decimal("0.50"),
        profit_budget=Decimal("0.10"),
    )
    apply_yes_stop_loss(state, cfg)
    assert state.phase == PairedBinaryPhase.STOP_PENDING_YES
    plan = static_survivor_target(Decimal("0.50"), Decimal("0.10"), cfg.slippage_buffer)
    assert state.no_planned_target == plan.planned_target
    assert state.no_target == plan.trigger_target


def test_no_stop_loss_sets_survivor_target() -> None:
    cfg = _cfg()
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.BOTH_LEGS_ACTIVE,
        yes_entry=Decimal("0.50"),
        no_entry=Decimal("0.50"),
        profit_budget=Decimal("0.10"),
    )
    apply_no_stop_loss(state, cfg)
    assert state.phase == PairedBinaryPhase.STOP_PENDING_NO
    plan = static_survivor_target(Decimal("0.50"), Decimal("0.10"), cfg.slippage_buffer)
    assert state.yes_planned_target == plan.planned_target
    assert state.yes_target == plan.trigger_target


def test_dual_stop_loss_tie_break_deterministic() -> None:
    leg = choose_dual_stop_leg(
        yes_loss=Decimal("0.05"),
        no_loss=Decimal("0.05"),
        yes_spread=Decimal("0.01"),
        no_spread=Decimal("0.01"),
    )
    assert leg == "no"


def test_persistence_path_uses_owner_id_and_market_id(tmp_path: Path) -> None:
    path = persistence_path(tmp_path, "paired_binary", "market-abc")
    assert path == tmp_path / "paired_binary" / "paired_binary" / "market-abc.json"
    state = PairedBinaryRuntimeState(
        owner_id="paired_binary",
        market_id="market-abc",
        phase=PairedBinaryPhase.BOTH_LEGS_ACTIVE,
        pair_correlation_id="paired_binary_test",
        yes_entry=Decimal("0.49"),
        no_entry=Decimal("0.51"),
        effective_qty=Decimal("5"),
    )
    save_persisted_state(path, state)
    loaded = load_persisted_state(path)
    assert loaded is not None
    assert loaded.phase == PairedBinaryPhase.BOTH_LEGS_ACTIVE
    assert loaded.yes_entry == Decimal("0.49")
