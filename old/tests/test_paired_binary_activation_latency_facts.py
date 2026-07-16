"""Activation path latency + decision_snapshot facts."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import WalletPosition
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import ShadowBootstrapConfig, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.market_data_runtime import inject_fixture_book
from tyrex_pm.runtime.paired_binary_run import _activate_monitoring_with_facts
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.entry_eval import read_leg_book
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"


@pytest.mark.asyncio
async def test_activation_emits_decision_snapshot_and_latency_chain(tmp_path: Path) -> None:
    app = parse_app_config(
        risk={"notional": {"min_usd": "0.01", "max_usd": "1000", "max_policy": "cap"}, "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"}, "venue_min_size": {"enabled": False}, "capital": {"enabled": False}},
        strategy={"kind": "paired_binary", "enabled": True, "paired_binary": {"owner_id": "paired_binary", "market_id": "m1", "yes_token_id": YES, "no_token_id": NO, "position_size": "5", "max_pair_entry_cost": "1.02", "max_spread_yes": "0.02", "max_spread_no": "0.02", "pair_stop_loss_pct": "0.04", "pair_take_profit_pct": "0.10", "slippage_buffer": "0.005", "use_fixture_book": True, "fixture_yes_bid": "0.48", "fixture_yes_ask": "0.49", "fixture_no_bid": "0.50", "fixture_no_ask": "0.51"}},
        runtime={"execution_mode": "shadow", "shadow_bootstrap": {"usdc_balance": "1", "usdc_allowance": "1"}, "market_data": {"enabled": True, "max_book_age_s": 5}, "execution": {"planner": {"enabled": True}}, "observability": {"emit_decision_snapshot": True}},
    )
    cfg = app.paired_binary
    assert cfg is not None
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    apply_shadow_bootstrap(coord.wallet, ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")))
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    inject_fixture_book(coord, YES, best_bid=Decimal("0.48"), best_ask=Decimal("0.49"))
    inject_fixture_book(coord, NO, best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(YES), Decimal("5"), correlation_id="y")
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(NO), Decimal("5"), correlation_id="n")
    coord.wallet.positions[TokenId(YES)] = WalletPosition(token_id=TokenId(YES), qty=Decimal("5"), avg_price_usd=Decimal("0.49"))
    coord.wallet.positions[TokenId(NO)] = WalletPosition(token_id=TokenId(NO), qty=Decimal("5"), avg_price_usd=Decimal("0.51"))
    yes_book = read_leg_book(coord.market_state, TokenId(YES), max_book_age_s=5.0)
    no_book = read_leg_book(coord.market_state, TokenId(NO), max_book_age_s=5.0)
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.BOTH_LEGS_FILLED,
        pair_correlation_id="pc-act",
        yes_entry=Decimal("0.49"),
        no_entry=Decimal("0.51"),
        effective_qty=Decimal("5"),
    )
    facts_path = tmp_path / "facts-act.jsonl"
    with JsonlSink(facts_path) as sink:
        await _activate_monitoring_with_facts(
            app=app,
            coord=coord,
            sink=sink,
            run_id=RunId("r-act"),
            state=state,
            cfg=cfg,
            yes_book=yes_book,
            no_book=no_book,
        )
    rows = [json.loads(x) for x in facts_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert any(r["fact_type"] == "decision_snapshot" and r["payload"]["decision_type"] == "activation" for r in rows)
    chains = [r for r in rows if r["fact_type"] == "latency_chain"]
    assert chains
    assert chains[0]["payload"].get("missing_fields_reason") is not None
