"""Open survivor must not silently shutdown at max_runtime."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import WalletPosition
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_PAIRED_BINARY_OPEN_EXPOSURE_AT_SHUTDOWN
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import SURVIVOR_ON_MAX_RUNTIME_FORCE, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore, make_snapshot
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.runtime.config import ShadowBootstrapConfig
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.exit_engine import ensure_pnl_budgets
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"


@pytest.mark.asyncio
async def test_live3_regression_no_silent_survivor_shutdown(tmp_path: Path) -> None:
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
            "enabled": True,
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
                "fixture_yes_bid": "0.43",
                "fixture_yes_ask": "0.46",
                "fixture_no_bid": "0.54",
                "fixture_no_ask": "0.57",
                "tick_interval_s": 0.005,
                "max_runtime_s": 0.01,
                "exit_order_style": "FAK",
            },
        },
        runtime={
            "execution_mode": "shadow",
            "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
            "reporting": {"enabled": True},
            "market_data": {"enabled": True, "max_book_age_s": 5},
            "execution": {"planner": {"enabled": True}},
            "observability": {"emit_decision_snapshot": False},
            "paired_binary": {
                "poll_interval_s": 0.005,
                "survivor_on_max_runtime": SURVIVOR_ON_MAX_RUNTIME_FORCE,
            },
        },
    )
    cfg = app.paired_binary
    assert cfg is not None
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    apply_shadow_bootstrap(
        coord.wallet,
        ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")),
    )
    store = MarketStateStore(default_max_age_s=5.0)
    coord.market_state = store
    from tyrex_pm.runtime.market_data_runtime import inject_fixture_book

    inject_fixture_book(coord, YES, best_bid=Decimal("0.43"), best_ask=Decimal("0.46"))
    inject_fixture_book(coord, NO, best_bid=Decimal("0.54"), best_ask=Decimal("0.57"))
    coord.wallet.positions[TokenId(NO)] = WalletPosition(
        token_id=TokenId(NO), qty=Decimal("5"), avg_price_usd=Decimal("0.47")
    )
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(NO), Decimal("5"), correlation_id="n1")

    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.ONLY_NO_ACTIVE,
        pair_correlation_id="paired_binary_live3",
        owner_id="paired_binary",
        market_id="m1",
        yes_token_id=YES,
        no_token_id=NO,
        yes_entry=Decimal("0.54"),
        no_entry=Decimal("0.47"),
        effective_qty=Decimal("5"),
        pair_opened_ts=monotonic_s(),
    )
    ensure_pnl_budgets(state, cfg)

    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        oms = ShadowOMS()
        await run_paired_binary_loop(
            app=app,
            run_id=RunId("live3-shutdown"),
            coord=coord,
            sink=sink,
            oms=oms,
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )
        facts_path = sink._path

    facts = [json.loads(ln) for ln in facts_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert any(f["fact_type"] == FACT_TYPE_PAIRED_BINARY_OPEN_EXPOSURE_AT_SHUTDOWN for f in facts)
    assert any(f["fact_type"] == "oms_submit" for f in facts)
    assert state.phase != PairedBinaryPhase.ONLY_NO_ACTIVE
    health = [f for f in facts if f.get("fact_type") == "health" and f["payload"].get("event") == "paired_binary_loop_stopped"]
    assert health
    assert health[-1]["payload"]["final_state"] != PairedBinaryPhase.ONLY_NO_ACTIVE.value
