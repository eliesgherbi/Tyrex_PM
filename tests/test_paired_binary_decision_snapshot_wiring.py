"""Paired-binary decision_snapshot wiring on material paths."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import ShadowBootstrapConfig, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.market_data_runtime import inject_fixture_book
from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore, make_snapshot
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.exit_engine import ensure_pnl_budgets
from tyrex_pm.strategies.paired_binary.monitor import PairedBinaryMonitor
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"


def _runtime(**over) -> dict:
    base = {
        "execution_mode": "shadow",
        "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
        "market_data": {"enabled": True},
        "execution": {"planner": {"enabled": True}},
        "observability": {"emit_decision_snapshot": True},
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
        "use_fixture_book": True,
        "fixture_yes_bid": "0.48",
        "fixture_yes_ask": "0.49",
        "fixture_no_bid": "0.50",
        "fixture_no_ask": "0.51",
        "entry_dry_run": True,
        "tick_interval_s": 0.05,
        "max_runtime_s": 2,
    }
    pb.update(pb_over)
    return {"kind": "paired_binary", "enabled": True, "paired_binary": pb}


def _coord(tmp_path: Path) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    apply_shadow_bootstrap(
        coord.wallet,
        ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")),
    )
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    return coord


def _facts(rows_path: Path) -> list[dict]:
    return [json.loads(x) for x in rows_path.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.mark.asyncio
async def test_entry_eval_emits_decision_snapshot(tmp_path: Path) -> None:
    app = parse_app_config(risk={"notional": {"min_usd": "0.01", "max_usd": "1000", "max_policy": "cap"}, "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"}, "venue_min_size": {"enabled": False}, "capital": {"enabled": False}}, strategy=_strategy(), runtime=_runtime())
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    inject_fixture_book(coord, YES, best_bid=Decimal("0.48"), best_ask=Decimal("0.49"))
    inject_fixture_book(coord, NO, best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))
    facts_path = tmp_path / "facts.jsonl"
    with JsonlSink(facts_path) as sink:
        await run_paired_binary_loop(
            app=app,
            run_id=RunId("r-entry"),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=PairedBinaryRuntimeState(phase=PairedBinaryPhase.IDLE),
            state_dir=tmp_path,
        )
    snaps = [r for r in _facts(facts_path) if r["fact_type"] == "decision_snapshot"]
    assert any(r["payload"]["decision_type"] == "entry_eval" for r in snaps)


@pytest.mark.asyncio
async def test_stop_trigger_emits_decision_snapshot(tmp_path: Path) -> None:
    app = parse_app_config(risk={"notional": {"min_usd": "0.01", "max_usd": "1000", "max_policy": "cap"}, "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"}, "venue_min_size": {"enabled": False}, "capital": {"enabled": False}, "inventory": {"sell_requires_venue_position": False}}, strategy=_strategy(entry_dry_run=False, stop_after_entry=True, max_runtime_s=3), runtime=_runtime())
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    inject_fixture_book(coord, YES, best_bid=Decimal("0.48"), best_ask=Decimal("0.49"))
    inject_fixture_book(coord, NO, best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(YES), Decimal("5"), correlation_id="y")
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(NO), Decimal("5"), correlation_id="n")
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.BOTH_LEGS_ACTIVE,
        pair_correlation_id="pc1",
        yes_entry=Decimal("0.49"),
        no_entry=Decimal("0.51"),
        yes_activation_bid=Decimal("0.48"),
        no_activation_bid=Decimal("0.50"),
        activation_ts=monotonic_s(),
        effective_qty=Decimal("5"),
        pair_opened_ts=monotonic_s(),
    )
    ensure_pnl_budgets(state, cfg)
    store = coord.market_state
    store.apply_snapshot(
        make_snapshot(TokenId(YES), bids=[(Decimal("0.43"), Decimal("100"))], asks=[(Decimal("0.44"), Decimal("100"))])
    )
    facts_path = tmp_path / "facts-stop.jsonl"
    monitor = PairedBinaryMonitor(cfg)
    with JsonlSink(facts_path) as sink:
        monitor.tick(coord, state, sink=sink, run_id=RunId("r-stop"), app=app)
    snaps = [r for r in _facts(facts_path) if r["fact_type"] == "decision_snapshot"]
    assert any(r["payload"]["decision_type"] == "stop_trigger" for r in snaps)
