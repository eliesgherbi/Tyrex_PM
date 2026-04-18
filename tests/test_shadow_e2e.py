from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from tyrex_pm.core.ids import RunId
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.ingestion.guru_stream import process_fixture_signals
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_GURU_SIGNAL,
    FACT_TYPE_INTENT,
    FACT_TYPE_OMS_SUBMIT,
    FACT_TYPE_RECONCILE,
    FACT_TYPE_RISK,
    FACT_TYPE_STRATEGY_SKIP,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import load_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.pipeline import process_new_guru_signals
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.strategy_store import StrategyStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.guru_follow.strategy import GuruFollowStrategy
from tyrex_pm.venue.polymarket.data_api_client import DataApiClient


async def _run_e2e(facts_path: Path) -> list[dict]:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root, scenario_file="shadow_guru")
    assert app.runtime.shadow_bootstrap is not None
    run_id = RunId(str(uuid4()))
    store = StrategyStore()
    fixture = root / "tests" / "fixtures" / "data_api" / "activity_batch.json"
    sigs = DataApiClient.parse_activity_json(fixture.read_text(encoding="utf-8"), "0xguru")
    new = process_fixture_signals(sigs, store)
    strat = GuruFollowStrategy(app.strategy)
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)
    with JsonlSink(facts_path) as sink:
        await process_new_guru_signals(
            new,
            app=app,
            run_id=run_id,
            strategy=strat,
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
        )
    return [json.loads(line) for line in facts_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_shadow_e2e_guru_to_oms_facts(tmp_path: Path) -> None:
    facts_path = tmp_path / "facts.jsonl"
    rows = asyncio.run(_run_e2e(facts_path))
    types = [r["fact_type"] for r in rows]

    assert types.count(FACT_TYPE_GURU_SIGNAL) == 2
    assert FACT_TYPE_STRATEGY_SKIP in types
    assert types.count(FACT_TYPE_INTENT) == 1
    assert types.count(FACT_TYPE_RISK) == 1
    assert types.count(FACT_TYPE_OMS_SUBMIT) == 1
    # Reconcile facts are now de-duplicated when consecutive runs produce the same operator-
    # relevant state tuple (drift flags, severity, tombstones, decision counts). The shadow
    # E2E exercises both an APPROVED path and a STRATEGY_SKIP path, which produce different
    # provisional-row counts and thus distinct signatures → still ≥1 reconcile, can be
    # ≥2 if the two states differ enough to trip the signature.
    assert types.count(FACT_TYPE_RECONCILE) >= 1

    oms_rows = [r for r in rows if r["fact_type"] == FACT_TYPE_OMS_SUBMIT]
    assert oms_rows[0]["payload"]["oms_result"] == "shadow_ack"

    act_a_corr = "act-a"
    guru_a = [r for r in rows if r["fact_type"] == FACT_TYPE_GURU_SIGNAL and r["correlation_id"] == act_a_corr]
    assert len(guru_a) == 1
    risk_a = [r for r in rows if r["fact_type"] == FACT_TYPE_RISK and r["correlation_id"] == act_a_corr]
    assert len(risk_a) == 1
    assert risk_a[0]["payload"]["approved"] is True
