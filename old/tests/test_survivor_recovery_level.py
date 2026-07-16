"""Pair breakeven / recovery level tests (Phase 1 simplified)."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.runtime.config import RecoveryLevelConfig, SurvivalConfig, parse_app_config
from tyrex_pm.strategies.paired_binary.state import LegRuntime, PairedBinaryRuntimeState
from tyrex_pm.survival.recovery_level import compute_recovery_level, setup_simplified_survivor_after_loser_exit


def _cfg():
    app = parse_app_config(
        risk={"notional": {"min_usd": "1", "max_usd": "100", "max_policy": "cap"}},
        runtime={
            "execution_mode": "shadow",
            "market_data": {"enabled": True},
            "execution": {"planner": {"enabled": True}},
        },
        strategy={
            "kind": "paired_binary",
            "paired_binary": {
                "owner_id": "pb",
                "market_id": "m1",
                "yes_token_id": "y",
                "no_token_id": "n",
                "position_size": "5",
                "max_pair_entry_cost": "1.02",
                "max_spread_yes": "0.05",
                "max_spread_no": "0.05",
                "pair_stop_loss_pct": "0.04",
                "pair_take_profit_pct": "0.10",
                "slippage_buffer": "0.005",
                "reject_if_spread_exceeds_loss_budget": False,
                "max_holding_time_s": 3600,
            },
        },
    )
    return app.paired_binary


def _state() -> PairedBinaryRuntimeState:
    return PairedBinaryRuntimeState(
        effective_qty=Decimal("5"),
        yes_entry=Decimal("0.48"),
        no_entry=Decimal("0.52"),
        yes=LegRuntime(entry_cash=Decimal("2.40"), entry_qty=Decimal("5")),
        no=LegRuntime(
            triggered=True,
            entry_cash=Decimal("2.60"),
            entry_qty=Decimal("5"),
            exit_cash=Decimal("2.00"),
            exit_qty=Decimal("5"),
        ),
    )


def test_recovery_level_formula() -> None:
    survival = SurvivalConfig(
        recovery_level=RecoveryLevelConfig(
            desired_buffer=Decimal("0"),
            slippage_buffer=Decimal("0.01"),
            activation_buffer=Decimal("0.02"),
        )
    )
    result = compute_recovery_level(_state(), _cfg(), survival, survivor_leg="yes")
    assert result is not None
    # (5.00 - 2.00 + 0 + 0.01) / 5 = 0.602
    assert result.breakeven_price == Decimal("0.602")
    assert result.activation_price == Decimal("0.622")


def test_setup_after_loser_exit_sets_floor_and_breakeven() -> None:
    state = _state()
    survival = SurvivalConfig()
    cfg = _cfg()
    setup = setup_simplified_survivor_after_loser_exit(
        state,
        cfg,
        survival,
        loser_leg="no",
        loser_exit_fill=Decimal("0.40"),
        survivor_bid=Decimal("0.55"),
    )
    assert setup is not None
    recovery, floor = setup
    assert state.survivor_leg_state is not None
    assert state.survivor_leg_state["hard_floor_price"] == str(floor)
    assert state.survivor_leg_state["breakeven_price"] == str(recovery.breakeven_price)
    assert state.yes_target == recovery.breakeven_price
