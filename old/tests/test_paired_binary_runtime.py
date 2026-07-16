"""Paired binary runtime integration tests (Phase 4.6)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import EnterIntent
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import ShadowBootstrapConfig, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.paired_binary_recovery import recover_on_startup
from tyrex_pm.runtime.paired_binary_run import _complete_entry_phase, run_paired_binary_loop
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.base import StrategyContext
from tyrex_pm.strategies.paired_binary import facts as pb_facts
from tyrex_pm.strategies.paired_binary.entry_eval import LegBook, read_leg_book
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from tyrex_pm.strategies.paired_binary.strategy import PairedBinaryStrategy

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"

_RISK = {
    "notional": {"min_usd": "0.01", "max_usd": "1000", "max_policy": "cap"},
    "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
    "venue_min_size": {"enabled": True, "default_min_size": "5"},
    "capital": {"enabled": False},
    "inventory": {"sell_requires_venue_position": False},
}


def _runtime(**over) -> dict:
    base = {
        "execution_mode": "shadow",
        "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
        "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
        "market_data": {"enabled": True, "max_book_age_s": 5},
        "execution": {"planner": {"enabled": True}},
        "paired_binary": {"poll_interval_s": 0.05},
    }
    base.update(over)
    return base


def _strategy(**pb_over) -> dict:
    pb = {
        "owner_id": "paired_binary",
        "market_id": "m1",
        "yes_token_id": YES,
        "no_token_id": NO,
        "position_size": "5",
        "max_pair_entry_cost": "1.02",
        "max_spread_yes": "0.02",
        "max_spread_no": "0.02",
        "pair_stop_loss_pct": "0.04",
        "pair_take_profit_pct": "0.10",
        "slippage_buffer": "0.005",
        "reject_if_spread_exceeds_loss_budget": True,
        "max_holding_time_s": 3600,
        "entry_fill_timeout_s": 5,
        "min_effective_pair_qty": "5",
        "use_fixture_book": True,
        "fixture_yes_bid": "0.48",
        "fixture_yes_ask": "0.49",
        "fixture_no_bid": "0.50",
        "fixture_no_ask": "0.51",
        "run_once": True,
        "tick_interval_s": 0.05,
        "max_runtime_s": 5,
        "stop_after_entry": True,
    }
    pb.update(pb_over)
    return {"kind": "paired_binary", "enabled": True, "paired_binary": pb}


def _app(**pb_over):
    return parse_app_config(risk=dict(_RISK), strategy=_strategy(**pb_over), runtime=_runtime())


def _coord(tmp_path: Path) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    apply_shadow_bootstrap(
        coord.wallet,
        ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")),
    )
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    return coord


def test_entry_creates_two_buy_intents(tmp_path: Path) -> None:
    app = _app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    from tyrex_pm.runtime.market_data_runtime import inject_fixture_book

    inject_fixture_book(coord, YES, best_bid=Decimal("0.48"), best_ask=Decimal("0.49"))
    inject_fixture_book(coord, NO, best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))
    strat = PairedBinaryStrategy(cfg)
    ctx = StrategyContext(coord=coord, market_state=coord.market_state)
    pairs, skip = strat.evaluate_entry(ctx, pair_correlation_id="pc_test")
    assert skip is None
    assert len(pairs) == 2
    intents = [p[0] for p in pairs]
    assert all(isinstance(i, EnterIntent) for i in intents)
    assert intents[0].side == Side.BUY
    sides = {str(i.token_id) for i in intents}
    assert sides == {YES, NO}


def test_entry_does_not_submit_directly(tmp_path: Path) -> None:
    app = _app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    from tyrex_pm.runtime.market_data_runtime import inject_fixture_book

    inject_fixture_book(coord, YES, best_bid=Decimal("0.48"), best_ask=Decimal("0.49"))
    inject_fixture_book(coord, NO, best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))
    strat = PairedBinaryStrategy(cfg)
    ctx = StrategyContext(coord=coord, market_state=coord.market_state)
    pairs, _ = strat.evaluate_entry(ctx, pair_correlation_id="pc_test")
    assert pairs
    assert coord.orders.orders == {}


def test_live_preflight_rejects_fixtures() -> None:
    from tyrex_pm.core.errors import ConfigError

    with pytest.raises(ConfigError, match="fixture"):
        parse_app_config(
            risk=dict(_RISK),
            strategy=_strategy(use_fixture_book=True),
            runtime=_runtime(execution_mode="live"),
        )


@pytest.mark.asyncio
async def test_shadow_paired_entry_reaches_both_legs_active(tmp_path: Path) -> None:
    app = _app(
        stop_after_entry=True,
        max_runtime_s=30,
        reject_if_spread_exceeds_loss_budget=False,
    )
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    run_id = RunId(str(uuid4()))
    runs = tmp_path / "runs" / str(run_id)
    runs.mkdir(parents=True)
    with JsonlSink(runs / "facts.jsonl") as sink:
        state = recover_on_startup(coord, cfg, state_dir=tmp_path, sink=sink, run_id=run_id)
        oms = ShadowOMS()
        ticks = await run_paired_binary_loop(
            app=app,
            run_id=run_id,
            coord=coord,
            sink=sink,
            oms=oms,
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
            apply_local_shadow_fill=True,
        )
    assert ticks >= 1
    assert state.phase in {
        PairedBinaryPhase.BOTH_LEGS_ACTIVE,
        PairedBinaryPhase.BOTH_LEGS_FILLED,
        PairedBinaryPhase.DONE,
    }
    lines = (runs / "facts.jsonl").read_text(encoding="utf-8").strip().splitlines()
    types = [json.loads(l)["fact_type"] for l in lines]
    assert "paired_binary_pair_preflight" in types
    assert "paired_binary_entry_submitted" in types or "paired_binary_pair_entry_committed" in types
    assert "oms_submit" in types
    assert "execution_plan" in types


@pytest.mark.asyncio
async def test_entry_dry_run_emits_eval_no_oms(tmp_path: Path) -> None:
    app = _app(entry_dry_run=True, stop_after_entry=False)
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    run_id = RunId(str(uuid4()))
    runs = tmp_path / "runs" / str(run_id)
    runs.mkdir(parents=True)
    with JsonlSink(runs / "facts.jsonl") as sink:
        state = PairedBinaryRuntimeState(
            owner_id=cfg.owner_id,
            market_id=cfg.market_id,
            yes_token_id=cfg.yes_token_id,
            no_token_id=cfg.no_token_id,
        )
        await run_paired_binary_loop(
            app=app,
            run_id=run_id,
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
            apply_local_shadow_fill=True,
        )
    lines = (runs / "facts.jsonl").read_text(encoding="utf-8").strip().splitlines()
    types = [json.loads(l)["fact_type"] for l in lines]
    assert "paired_binary_entry_eval" in types or "paired_binary_entry_skip" in types
    assert "oms_submit" not in types


def test_restart_recovery_both_legs(tmp_path: Path) -> None:
    app = _app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(YES), Decimal("5"), correlation_id="y")
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(NO), Decimal("5"), correlation_id="n")
    state = recover_on_startup(coord, cfg, state_dir=tmp_path)
    assert state.phase == PairedBinaryPhase.BOTH_LEGS_FILLED


def test_restart_recovery_only_yes(tmp_path: Path) -> None:
    app = _app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(YES), Decimal("5"), correlation_id="y")
    state = recover_on_startup(coord, cfg, state_dir=tmp_path)
    assert state.phase == PairedBinaryPhase.ONLY_YES_ACTIVE


def test_restart_recovery_only_no(tmp_path: Path) -> None:
    app = _app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(NO), Decimal("5"), correlation_id="n")
    state = recover_on_startup(coord, cfg, state_dir=tmp_path)
    assert state.phase == PairedBinaryPhase.ONLY_NO_ACTIVE


def test_facts_deduped(tmp_path: Path) -> None:
    state = PairedBinaryRuntimeState(owner_id="pb", market_id="m", yes_token_id=YES, no_token_id=NO)
    yes = LegBook(TokenId(YES), Decimal("0.48"), Decimal("0.49"), False)
    no = LegBook(TokenId(NO), Decimal("0.50"), Decimal("0.51"), False)
    run_id = RunId(str(uuid4()))
    runs = tmp_path / "runs"
    runs.mkdir(parents=True)
    with JsonlSink(runs / "facts.jsonl") as sink:
        pb_facts.emit_entry_skip(sink, run_id, state, yes, no, reason="x", pair_cost=Decimal("1"), yes_spread=Decimal("0.01"), no_spread=Decimal("0.01"))
        pb_facts.emit_entry_skip(sink, run_id, state, yes, no, reason="x", pair_cost=Decimal("1"), yes_spread=Decimal("0.01"), no_spread=Decimal("0.01"))
    lines = (runs / "facts.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_paired_strategy_uses_market_state_store(tmp_path: Path) -> None:
    app = _app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    from tyrex_pm.runtime.market_data_runtime import inject_fixture_book

    inject_fixture_book(coord, YES, best_bid=Decimal("0.48"), best_ask=Decimal("0.49"))
    inject_fixture_book(coord, NO, best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))
    yes = read_leg_book(coord.market_state, TokenId(YES), max_book_age_s=5.0)
    assert yes.ask == Decimal("0.49")


@pytest.mark.asyncio
async def test_partial_yes_fill_no_fail_triggers_unwind(tmp_path: Path) -> None:
    app = _app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(YES), Decimal("5"), correlation_id="y")
    coord.wallet.positions[TokenId(YES)] = __import__(
        "tyrex_pm.core.models", fromlist=["WalletPosition"]
    ).WalletPosition(token_id=TokenId(YES), qty=Decimal("5"), avg_price_usd=Decimal("0.49"))
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.BOTH_ENTRY_PENDING,
        pair_correlation_id="pc_unwind",
        owner_id=cfg.owner_id,
        market_id=cfg.market_id,
        yes_token_id=cfg.yes_token_id,
        no_token_id=cfg.no_token_id,
    )
    from tyrex_pm.runtime.market_data_runtime import inject_fixture_book

    inject_fixture_book(coord, YES, best_bid=Decimal("0.48"), best_ask=Decimal("0.49"))
    inject_fixture_book(coord, NO, best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))
    strat = PairedBinaryStrategy(cfg)
    runs = tmp_path / "runs"
    runs.mkdir(parents=True)
    with JsonlSink(runs / "facts.jsonl") as sink:
        await _complete_entry_phase(
            app=app,
            run_id=RunId(str(uuid4())),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            strategy=strat,
            cfg=cfg,
            state=state,
            apply_local_shadow_fill=True,
            live_clob_client=None,
        )
    assert state.phase == PairedBinaryPhase.FAILED


@pytest.mark.asyncio
async def test_sell_sizes_clamp_to_allocation(tmp_path: Path) -> None:
    from tyrex_pm.strategies.paired_binary.sizing import clamp_exit_size

    coord = _coord(tmp_path)
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(YES), Decimal("3"), correlation_id="y")
    coord.wallet.positions[TokenId(YES)] = __import__(
        "tyrex_pm.core.models", fromlist=["WalletPosition"]
    ).WalletPosition(token_id=TokenId(YES), qty=Decimal("3"), avg_price_usd=Decimal("0.49"))
    sizing = clamp_exit_size(
        coord,
        owner_id="paired_binary",
        token_id=TokenId(YES),
        planned=Decimal("5"),
        exit_order_style=OrderStyle.FAK,
    )
    assert sizing.final_size == Decimal("3")
