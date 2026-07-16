"""Paired-binary event wake integration."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import ShadowBootstrapConfig, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.market_update_coordinator import (
    MarketUpdateCoordinator,
    attach_coordinator_to_authoritative_store,
)
from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import BookLevel, MarketStateStore
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.core.models import WalletPosition
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.strategies.paired_binary.exit_engine import ensure_pnl_budgets
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from tyrex_pm.market_data.models import BookSource

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"


def _app(**pb_over):
    return parse_app_config(
        risk={
            "notional": {"min_usd": "0.01", "max_usd": "1000", "max_policy": "cap"},
            "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
            "venue_min_size": {"enabled": False},
            "capital": {"enabled": False},
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
                "tick_interval_s": 0.2,
                "max_runtime_s": 2,
                **pb_over,
            },
        },
        runtime={
            "execution_mode": "shadow",
            "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
            "market_data": {"enabled": True, "max_book_age_s": 5},
            "execution": {"planner": {"enabled": True}},
            "paired_binary": {"poll_interval_s": 0.15, "max_decision_rate_per_market_ms": 50},
        },
    )


@pytest.mark.asyncio
async def test_store_update_emits_event_wake_tick_source(tmp_path: Path) -> None:
    app = _app(
        entry_dry_run=False,
        stop_after_entry=False,
        max_runtime_s=2,
        tick_interval_s=0.5,
    )
    cfg = app.paired_binary
    assert cfg is not None
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    apply_shadow_bootstrap(
        coord.wallet,
        ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")),
    )
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    coordinator = MarketUpdateCoordinator(debounce_ms=30)
    attach_coordinator_to_authoritative_store(coord, coordinator)
    coord.market_update_coordinator = coordinator

    from tyrex_pm.runtime.market_data_runtime import inject_fixture_book

    inject_fixture_book(coord, YES, best_bid=Decimal("0.48"), best_ask=Decimal("0.49"))
    inject_fixture_book(coord, NO, best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(YES), Decimal("5"), correlation_id="y")
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(NO), Decimal("5"), correlation_id="n")
    coord.wallet.positions[TokenId(YES)] = WalletPosition(
        token_id=TokenId(YES), qty=Decimal("5"), avg_price_usd=Decimal("0.49")
    )
    coord.wallet.positions[TokenId(NO)] = WalletPosition(
        token_id=TokenId(NO), qty=Decimal("5"), avg_price_usd=Decimal("0.51")
    )

    facts_path = tmp_path / "facts.jsonl"
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.BOTH_LEGS_ACTIVE,
        pair_correlation_id="pc-wake",
        yes_entry=Decimal("0.49"),
        no_entry=Decimal("0.51"),
        yes_activation_bid=Decimal("0.48"),
        no_activation_bid=Decimal("0.50"),
        activation_ts=monotonic_s(),
        effective_qty=Decimal("5"),
        pair_opened_ts=monotonic_s(),
    )
    ensure_pnl_budgets(state, cfg)
    stop = asyncio.Event()

    async def bump_books() -> None:
        await asyncio.sleep(0.05)
        store = coord.market_state
        store.apply_book(
            TokenId(YES),
            [BookLevel(Decimal("0.48"), Decimal("100"))],
            [BookLevel(Decimal("0.49"), Decimal("100"))],
            source=BookSource.FIXTURE,
        )

    bump = asyncio.create_task(bump_books())
    with JsonlSink(facts_path) as sink:
        await run_paired_binary_loop(
            app=app,
            run_id=RunId("r-wake"),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
            stop=stop,
        )
    await bump
    rows = [json.loads(x) for x in facts_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    tick_facts = [r for r in rows if r.get("fact_type") == "paired_binary_tick_source"]
    assert any(r["payload"]["tick_source"] == "event_wake" for r in tick_facts)
