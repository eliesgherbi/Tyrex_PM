"""Survival enforce-mode OMS dispatch tests."""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.models import WalletPosition
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.market_data_runtime import inject_fixture_book
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.monitor import PairedBinaryMonitor
from tyrex_pm.strategies.paired_binary.state import LegRuntime, PairedBinaryPhase, PairedBinaryRuntimeState

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"


def _app_cfg(*, trailing_mode: str = "advisory", floor_mode: str = "advisory") -> object:
    return parse_app_config(
        risk={
            "notional": {"min_usd": "1", "max_usd": "100", "max_policy": "cap"},
            "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
            "venue_min_size": {"enabled": False},
            "capital": {"enabled": False},
            "inventory": {"sell_requires_venue_position": False},
            "concurrency": {"max_orders_in_flight": 10},
            "readiness": {"require_wallet_sync": False},
        },
        strategy={
            "kind": "paired_binary",
            "paired_binary": {
                "owner_id": "paired_binary",
                "market_id": "m1",
                "yes_token_id": YES,
                "no_token_id": NO,
                "position_size": "5",
                "max_pair_entry_cost": "1.02",
                "max_spread_yes": "0.05",
                "max_spread_no": "0.05",
                "pair_stop_loss_pct": "0.04",
                "pair_take_profit_pct": "0.10",
                "slippage_buffer": "0.005",
                "reject_if_spread_exceeds_loss_budget": False,
                "max_holding_time_s": 3600,
                "use_fixture_book": True,
                "tick_interval_s": 0.005,
                "max_runtime_s": 0.01,
                "exit_order_style": "FAK",
            },
        },
        runtime={
            "execution_mode": "shadow",
            "market_data": {"enabled": True, "max_book_age_s": 5},
            "execution": {"planner": {"enabled": True, "use_executable_depth": True}},
            "survival": {
                "enabled": True,
                "trailing_stop": {
                    "enabled": True,
                    "enforcement_mode": trailing_mode,
                    "activation_mode": "executable_gain",
                    "arm_delay_s": 0,
                    "arm_after_executable_gain": Decimal("0.01"),
                    "trail_distance": Decimal("0.02"),
                    "min_depth_fraction": Decimal("0.5"),
                },
                "survivor_floor": {
                    "enabled": True,
                    "enforcement_mode": floor_mode,
                },
            },
        },
    )


def _coord(*, yes_bid: str = "0.57") -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    coord.allocation_ledger = AllocationLedger(path=Path("var/test-ledger-enforce-dispatch.json"))
    bid = Decimal(yes_bid)
    inject_fixture_book(coord, YES, best_bid=bid, best_ask=bid + Decimal("0.01"))
    inject_fixture_book(coord, NO, best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(YES), Decimal("5"), correlation_id="y")
    coord.wallet.positions[TokenId(YES)] = WalletPosition(
        token_id=TokenId(YES), qty=Decimal("5"), avg_price_usd=Decimal("0.48")
    )
    return coord


def _survivor_state(*, trailing_armed: bool = True) -> PairedBinaryRuntimeState:
    trailing = {
        "state": "armed" if trailing_armed else "disarmed",
        "peak_executable_bid": "0.60",
        "trail_floor": "0.58",
        "armed_at_ts": time.time() - 30,
    }
    return PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.ONLY_YES_ACTIVE,
        effective_qty=Decimal("5"),
        yes_entry=Decimal("0.48"),
        no_entry=Decimal("0.52"),
        yes_target=Decimal("0.70"),
        yes_trigger_stop=Decimal("0.40"),
        no_trigger_stop=Decimal("0.40"),
        pair_cost=Decimal("1.00"),
        yes=LegRuntime(
            triggered=False,
            entry_cash=Decimal("2.40"),
            entry_qty=Decimal("5"),
        ),
        no=LegRuntime(
            triggered=True,
            entry_cash=Decimal("2.60"),
            entry_qty=Decimal("5"),
            exit_cash=Decimal("2.00"),
            exit_qty=Decimal("5"),
        ),
        survivor_leg_state={
            "survivor_bid_0": "0.50",
            "selected_target": "0.70",
            "selected_mode": "full_recovery",
            "loser_exit_ts": time.time() - 120,
            "seconds_to_close_0": 600.0,
            "available_survival_time": 580.0,
            "trailing_stop": trailing,
        },
    )


def test_advisory_mode_submits_no_survival_oms(tmp_path: Path) -> None:
    app = _app_cfg(trailing_mode="advisory")
    cfg = app.paired_binary
    monitor = PairedBinaryMonitor(cfg)
    facts_path = tmp_path / "facts.jsonl"
    with JsonlSink(facts_path) as sink:
        work = monitor.tick(
            _coord(),
            _survivor_state(),
            sink=sink,
            run_id=RunId("adv"),
            app=app,
        )
    assert work == []
    types = {json.loads(l)["fact_type"] for l in facts_path.read_text().splitlines() if l.strip()}
    assert "survival_enforce_exit_submitted" not in types


def test_trailing_enforce_submits_reduce_only_exit(tmp_path: Path) -> None:
    app = _app_cfg(trailing_mode="enforce")
    cfg = app.paired_binary
    monitor = PairedBinaryMonitor(cfg)
    facts_path = tmp_path / "facts.jsonl"
    with JsonlSink(facts_path) as sink:
        work = monitor.tick(
            _coord(yes_bid="0.57"),
            _survivor_state(),
            sink=sink,
            run_id=RunId("enforce-trail"),
            app=app,
        )
    assert len(work) == 1
    assert work[0].intent.side.value == "SELL"
    types = {json.loads(l)["fact_type"] for l in facts_path.read_text().splitlines() if l.strip()}
    assert "survival_enforce_exit_requested" in types
    assert "survival_enforce_exit_submitted" in types


def test_duplicate_enforce_tick_does_not_resubmit(tmp_path: Path) -> None:
    app = _app_cfg(trailing_mode="enforce")
    cfg = app.paired_binary
    monitor = PairedBinaryMonitor(cfg)
    state = _survivor_state()
    coord = _coord(yes_bid="0.57")
    facts_path = tmp_path / "facts.jsonl"
    with JsonlSink(facts_path) as sink:
        work1 = monitor.tick(coord, state, sink=sink, run_id=RunId("dup1"), app=app)
        work2 = monitor.tick(coord, state, sink=sink, run_id=RunId("dup2"), app=app)
    assert len(work1) == 1
    assert work2 == []
    submitted = [
        json.loads(l)
        for l in facts_path.read_text().splitlines()
        if l.strip() and json.loads(l)["fact_type"] == "survival_enforce_exit_submitted"
    ]
    assert len(submitted) == 1


def test_hard_floor_enforce_submits_reduce_only_exit(tmp_path: Path) -> None:
    app = parse_app_config(
        risk={
            "notional": {"min_usd": "1", "max_usd": "100", "max_policy": "cap"},
            "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
            "venue_min_size": {"enabled": False},
            "capital": {"enabled": False},
            "inventory": {"sell_requires_venue_position": False},
            "concurrency": {"max_orders_in_flight": 10},
            "readiness": {"require_wallet_sync": False},
        },
        strategy={
            "kind": "paired_binary",
            "paired_binary": {
                "owner_id": "paired_binary",
                "market_id": "m1",
                "yes_token_id": YES,
                "no_token_id": NO,
                "position_size": "5",
                "max_pair_entry_cost": "1.02",
                "max_spread_yes": "0.05",
                "max_spread_no": "0.05",
                "pair_stop_loss_pct": "0.04",
                "pair_take_profit_pct": "0.10",
                "slippage_buffer": "0.005",
                "reject_if_spread_exceeds_loss_budget": False,
                "max_holding_time_s": 3600,
                "use_fixture_book": True,
                "tick_interval_s": 0.005,
                "max_runtime_s": 0.01,
                "exit_order_style": "FAK",
            },
        },
        runtime={
            "execution_mode": "shadow",
            "market_data": {"enabled": True, "max_book_age_s": 5},
            "execution": {"planner": {"enabled": True, "use_executable_depth": True}},
            "survival": {
                "enabled": True,
                "trailing_stop": {"enabled": False},
                "survivor_floor": {
                    "enabled": True,
                    "enforcement_mode": "enforce",
                    "require_fresh_book": False,
                },
            },
        },
    )
    cfg = app.paired_binary
    monitor = PairedBinaryMonitor(cfg)
    state = _survivor_state(trailing_armed=False)
    state.survivor_leg_state = {
        "hard_floor_price": "0.48",
        "breakeven_price": "0.60",
        "loser_exit_ts": time.time() - 120,
    }
    facts_path = tmp_path / "facts.jsonl"
    with JsonlSink(facts_path) as sink:
        work = monitor.tick(
            _coord(yes_bid="0.47"),
            state,
            sink=sink,
            run_id=RunId("floor-enforce"),
            app=app,
        )
    assert len(work) == 1
    assert work[0].intent.side.value == "SELL"
    types = {json.loads(l)["fact_type"] for l in facts_path.read_text().splitlines() if l.strip()}
    assert "survival_enforce_exit_requested" in types


def test_stall_logic_not_emitted_in_simplified_flow(tmp_path: Path) -> None:
    app = _app_cfg(trailing_mode="advisory")
    cfg = app.paired_binary
    monitor = PairedBinaryMonitor(cfg)
    state = _survivor_state(trailing_armed=False)
    facts_path = tmp_path / "facts.jsonl"
    with JsonlSink(facts_path) as sink:
        monitor.tick(_coord(yes_bid="0.50"), state, sink=sink, run_id=RunId("no-stall"), app=app)
    types = {json.loads(l)["fact_type"] for l in facts_path.read_text().splitlines() if l.strip()}
    assert "survivor_stall_detected" not in types
    assert "survivor_target_selected" not in types
