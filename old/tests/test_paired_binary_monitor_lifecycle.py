"""Paired binary monitor lifecycle hardening tests (Phase 4.6)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import ExecutionMode, OrderStyle
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import WalletPosition
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import PairedBinaryStrategyConfig, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore, make_snapshot
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.entry_eval import read_leg_book
from tyrex_pm.strategies.paired_binary.exit_engine import (
    both_legs_sellable,
    confirm_exit_submitted,
    ensure_pnl_budgets,
    evaluate_dual_stop,
)
from tyrex_pm.strategies.paired_binary.lifecycle import mark_both_legs_filled
from tyrex_pm.strategies.paired_binary.monitor import PairedBinaryMonitor
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
        pair_stop_loss_pct=Decimal("0.04"),
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
        tick_interval_s=0.01,
        max_book_age_s=5.0,
    )
    base.update(over)
    return PairedBinaryStrategyConfig(**base)


def _arm_state(state: PairedBinaryRuntimeState, cfg: PairedBinaryStrategyConfig) -> None:
    ensure_pnl_budgets(state, cfg)


def _coord(tmp_path: Path, *, wallet_qty: Decimal = Decimal("5")) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    store = MarketStateStore(default_max_age_s=5.0)
    store.apply_snapshot(
        make_snapshot(TokenId(YES), bids=[(Decimal("0.48"), Decimal("100"))], asks=[(Decimal("0.49"), Decimal("100"))])
    )
    store.apply_snapshot(
        make_snapshot(TokenId(NO), bids=[(Decimal("0.50"), Decimal("100"))], asks=[(Decimal("0.51"), Decimal("100"))])
    )
    coord.market_state = store
    if wallet_qty > 0:
        coord.wallet.positions[TokenId(YES)] = WalletPosition(
            token_id=TokenId(YES), qty=wallet_qty, avg_price_usd=Decimal("0.49")
        )
        coord.wallet.positions[TokenId(NO)] = WalletPosition(
            token_id=TokenId(NO), qty=wallet_qty, avg_price_usd=Decimal("0.51")
        )
    return coord


def _seed_ledger(coord: RuntimeCoordinator, qty: Decimal = Decimal("5")) -> None:
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(YES), qty, correlation_id="seed-y")
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(NO), qty, correlation_id="seed-n")


def _active_state(*, cfg: PairedBinaryStrategyConfig | None = None, **over) -> PairedBinaryRuntimeState:
    now = monotonic_s()
    active_cfg = cfg or _cfg()
    base = dict(
        phase=PairedBinaryPhase.BOTH_LEGS_ACTIVE,
        pair_correlation_id="pc1",
        yes_entry=Decimal("0.49"),
        no_entry=Decimal("0.51"),
        yes_activation_bid=Decimal("0.48"),
        no_activation_bid=Decimal("0.50"),
        activation_ts=now - 10.0,
        effective_qty=Decimal("5"),
        pair_opened_ts=now,
    )
    base.update(over)
    st = PairedBinaryRuntimeState(**base)
    _arm_state(st, active_cfg)
    return st


def _books(coord: RuntimeCoordinator):
    yes = read_leg_book(coord.market_state, TokenId(YES), max_book_age_s=5.0)
    no = read_leg_book(coord.market_state, TokenId(NO), max_book_age_s=5.0)
    return yes, no


def test_monitor_does_not_start_until_both_legs_sellable(tmp_path: Path) -> None:
    coord = _coord(tmp_path, wallet_qty=Decimal("0"))
    _seed_ledger(coord)
    cfg = _cfg()
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.BOTH_LEGS_FILLED,
        pair_correlation_id="pc1",
        owner_id=cfg.owner_id,
        market_id=cfg.market_id,
        yes_token_id=YES,
        no_token_id=NO,
        yes_entry=Decimal("0.49"),
        no_entry=Decimal("0.51"),
        effective_qty=Decimal("5"),
    )
    ok, _, _ = both_legs_sellable(coord, cfg, state)
    assert ok is False
    monitor = PairedBinaryMonitor(cfg)
    assert monitor.tick(coord, state) == []


def test_pair_stop_not_triggered_when_bid_above_trigger(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    _seed_ledger(coord)
    cfg = _cfg(pair_stop_loss_pct=Decimal("0.02"))
    state = _active_state(
        cfg=cfg,
        yes_entry=Decimal("0.49"),
        no_entry=Decimal("0.51"),
        yes_activation_bid=Decimal("0.48"),
        no_activation_bid=Decimal("0.50"),
    )
    coord.market_state.apply_snapshot(
        make_snapshot(TokenId(NO), bids=[(Decimal("0.50"), Decimal("100"))], asks=[(Decimal("0.51"), Decimal("100"))])
    )
    assert evaluate_dual_stop(state, cfg, *_books(coord)) is None


def test_tight_stop_triggers_when_bid_below_trigger(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    _seed_ledger(coord)
    cfg = _cfg(pair_stop_loss_pct=Decimal("0.02"))
    state = _active_state(cfg=cfg, yes_activation_bid=Decimal("0.48"), no_activation_bid=Decimal("0.50"))
    coord.market_state.apply_snapshot(
        make_snapshot(TokenId(YES), bids=[(Decimal("0.44"), Decimal("100"))], asks=[(Decimal("0.45"), Decimal("100"))])
    )
    assert evaluate_dual_stop(state, cfg, *_books(coord)) == "yes"


def test_trigger_without_sellable_inventory_does_not_enter_exiting(tmp_path: Path) -> None:
    coord = _coord(tmp_path, wallet_qty=Decimal("0"))
    _seed_ledger(coord)
    cfg = _cfg(pair_stop_loss_pct=Decimal("0.04"))
    state = _active_state()
    coord.market_state.apply_snapshot(
        make_snapshot(TokenId(YES), bids=[(Decimal("0.43"), Decimal("100"))], asks=[(Decimal("0.44"), Decimal("100"))])
    )
    monitor = PairedBinaryMonitor(cfg)
    work = monitor.tick(coord, state)
    assert work == []
    assert state.phase == PairedBinaryPhase.STOP_PENDING_YES


def test_trigger_without_sellable_inventory_retries_when_inventory_appears(tmp_path: Path) -> None:
    coord = _coord(tmp_path, wallet_qty=Decimal("0"))
    _seed_ledger(coord)
    cfg = _cfg(pair_stop_loss_pct=Decimal("0.04"))
    state = _active_state()
    state.phase = PairedBinaryPhase.STOP_PENDING_YES
    state.yes.pending_trigger_type = "stop_loss"
    coord.market_state.apply_snapshot(
        make_snapshot(TokenId(YES), bids=[(Decimal("0.43"), Decimal("100"))], asks=[(Decimal("0.44"), Decimal("100"))])
    )
    monitor = PairedBinaryMonitor(cfg)
    assert monitor.tick(coord, state) == []
    coord.wallet.positions[TokenId(YES)] = WalletPosition(
        token_id=TokenId(YES), qty=Decimal("5"), avg_price_usd=Decimal("0.49")
    )
    work = monitor.tick(coord, state)
    assert len(work) == 1
    assert state.phase == PairedBinaryPhase.STOP_PENDING_YES


def test_exiting_state_requires_exit_submission(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    _seed_ledger(coord)
    cfg = _cfg(pair_stop_loss_pct=Decimal("0.04"))
    state = _active_state()
    coord.market_state.apply_snapshot(
        make_snapshot(TokenId(YES), bids=[(Decimal("0.43"), Decimal("100"))], asks=[(Decimal("0.44"), Decimal("100"))])
    )
    monitor = PairedBinaryMonitor(cfg)
    work = monitor.tick(coord, state)
    assert work
    assert state.phase == PairedBinaryPhase.STOP_PENDING_YES
    confirm_exit_submitted(state, "yes")
    assert state.phase == PairedBinaryPhase.EXITING_YES


def test_timeout_handles_stop_pending_state(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    _seed_ledger(coord)
    cfg = _cfg(max_holding_time_s=0.001)
    state = _active_state(pair_opened_ts=0.0, activation_ts=0.0)
    state.phase = PairedBinaryPhase.STOP_PENDING_NO
    state.no.pending_trigger_type = "stop_loss"
    state.yes_target = Decimal("0.55")
    monitor = PairedBinaryMonitor(cfg)
    work = monitor.tick(coord, state)
    assert len(work) == 1
    assert state.phase in {PairedBinaryPhase.STOP_PENDING_NO, PairedBinaryPhase.TIMEOUT_PENDING}


def test_timeout_handles_exiting_state_without_order(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    _seed_ledger(coord)
    cfg = _cfg(max_holding_time_s=0.001)
    state = _active_state(pair_opened_ts=0.0, activation_ts=0.0)
    state.phase = PairedBinaryPhase.EXITING_NO
    state.no.exit_submitted = False
    monitor = PairedBinaryMonitor(cfg)
    work = monitor.tick(coord, state)
    assert work
    assert state.phase == PairedBinaryPhase.STOP_PENDING_NO


def test_tp_still_evaluated_after_loser_exit_submission_failure_or_retry(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    _seed_ledger(coord)
    cfg = _cfg()
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.ONLY_YES_ACTIVE,
        pair_correlation_id="pc1",
        yes_entry=Decimal("0.49"),
        yes_target=Decimal("0.55"),
        effective_qty=Decimal("5"),
        pair_opened_ts=monotonic_s(),
        activation_ts=monotonic_s() - 10,
    )
    coord.market_state.apply_snapshot(
        make_snapshot(TokenId(YES), bids=[(Decimal("0.56"), Decimal("100"))], asks=[(Decimal("0.57"), Decimal("100"))])
    )
    monitor = PairedBinaryMonitor(cfg)
    work = monitor.tick(coord, state)
    assert len(work) == 1
    assert state.phase == PairedBinaryPhase.TP_PENDING_YES


def test_dual_stop_fires_when_both_legs_below_trigger(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    _seed_ledger(coord)
    cfg = _cfg(pair_stop_loss_pct=Decimal("0.04"))
    state = _active_state(activation_ts=monotonic_s())
    coord.market_state.apply_snapshot(
        make_snapshot(TokenId(YES), bids=[(Decimal("0.43"), Decimal("100"))], asks=[(Decimal("0.44"), Decimal("100"))])
    )
    coord.market_state.apply_snapshot(
        make_snapshot(TokenId(NO), bids=[(Decimal("0.46"), Decimal("100"))], asks=[(Decimal("0.47"), Decimal("100"))])
    )
    assert evaluate_dual_stop(state, cfg, *_books(coord)) == "yes"


_RISK = {
    "notional": {"min_usd": "0.01", "max_usd": "1000", "max_policy": "cap"},
    "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
    "venue_min_size": {"enabled": False},
    "capital": {"enabled": False},
    "inventory": {"sell_requires_venue_position": False},
}


def _app(**pb_over):
    return parse_app_config(
        risk=dict(_RISK),
        strategy={
            "kind": "paired_binary",
            "paired_binary": {
                "owner_id": "paired_binary",
                "market_id": "m1",
                "yes_token_id": YES,
                "no_token_id": NO,
                "position_size": "5",
                "use_fixture_book": True,
                "fixture_yes_bid": "0.48",
                "fixture_yes_ask": "0.49",
                "fixture_no_bid": "0.50",
                "fixture_no_ask": "0.51",
                "stop_after_entry": True,
                "tick_interval_s": 0.01,
                "max_runtime_s": 1,
                **pb_over,
            },
        },
        runtime={
            "execution_mode": "shadow",
            "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
            "market_data": {"enabled": True, "max_book_age_s": 5},
            "execution": {"planner": {"enabled": True}},
        },
    )

@pytest.mark.asyncio
async def test_sellability_gate_emits_monitor_started(tmp_path: Path) -> None:
    app = _app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    _seed_ledger(coord)
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.BOTH_LEGS_FILLED,
        pair_correlation_id="pc_gate",
        owner_id=cfg.owner_id,
        market_id=cfg.market_id,
        yes_token_id=YES,
        no_token_id=NO,
    )
    mark_both_legs_filled(
        state,
        yes_qty=Decimal("5"),
        no_qty=Decimal("5"),
        yes_entry=Decimal("0.49"),
        no_entry=Decimal("0.51"),
        entry_price_source="test",
    )
    run_id = RunId(str(uuid4()))
    runs = tmp_path / "runs" / str(run_id)
    runs.mkdir(parents=True)
    with JsonlSink(runs / "facts.jsonl") as sink:
        await run_paired_binary_loop(
            app=app,
            run_id=run_id,
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path / "state",
            apply_local_shadow_fill=True,
        )
    types = [json.loads(l)["fact_type"] for l in (runs / "facts.jsonl").read_text().splitlines()]
    assert state.phase == PairedBinaryPhase.BOTH_LEGS_ACTIVE
    assert "paired_binary_activation_reference" in types
    assert "paired_binary_pnl_plan" in types
    assert "paired_binary_monitor_started" in types
