from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import ClientOrderId, RunId, TokenId
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.ingestion.guru_stream import process_fixture_signals
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import load_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.pipeline import process_new_guru_signals, reconcile_coordinator
from tyrex_pm.state.order_store import LocalOrder, OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.strategy_store import StrategyStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.guru_follow.strategy import GuruFollowStrategy
from tyrex_pm.venue.polymarket.data_api_client import DataApiClient


@pytest.mark.asyncio
async def test_reconcile_drift_denies_subsequent_risk(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root, scenario_file="shadow_guru")
    assert app.runtime.shadow_bootstrap is not None
    run_id = RunId(str(uuid4()))
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)

    ghost = ClientOrderId("ghost-resting")
    coord.orders.orders[ghost] = LocalOrder(
        client_order_id=ghost,
        venue_order_id=None,
        token_id=TokenId("1234567890"),
        side=Side.BUY,
        remaining=Decimal("1"),
    )

    facts_path = tmp_path / "facts.jsonl"
    strat = GuruFollowStrategy(app.strategy)
    store = StrategyStore()
    fixture = root / "tests" / "fixtures" / "data_api" / "activity_batch.json"
    sigs = DataApiClient.parse_activity_json(fixture.read_text(encoding="utf-8"), "0xguru")
    new = process_fixture_signals(sigs, store)

    with JsonlSink(facts_path) as sink:
        reconcile_coordinator(coord, sink, str(run_id))
        await process_new_guru_signals(
            new,
            app=app,
            run_id=run_id,
            strategy=strat,
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
        )

    rows = [json.loads(line) for line in facts_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    risks = [r for r in rows if r["fact_type"] == "risk_decision" and r["correlation_id"] == "act-a"]
    assert risks
    assert risks[0]["payload"]["approved"] is False
    assert "reconcile_drift" in risks[0]["payload"]["reason_codes"]
