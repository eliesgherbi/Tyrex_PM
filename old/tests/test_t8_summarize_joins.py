"""
T8 — Summarize + join audit on a real shadow run directory (parity-grade).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from tyrex_pm.core.ids import RunId
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.ingestion.guru_stream import process_fixture_signals
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
from tyrex_pm.reporting.summarize import audit_fact_joins, summarize_run
from tyrex_pm.venue.polymarket.data_api_client import DataApiClient


def test_t8_summarize_join_audit_on_real_run_dir(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    run_dir = tmp_path / "run_shadow"
    run_dir.mkdir(parents=True)

    app = load_app_config(repo_root=root, scenario_file="shadow_guru")
    assert app.runtime.shadow_bootstrap is not None
    fixture = root / "tests" / "fixtures" / "data_api" / "activity_batch.json"
    sigs = DataApiClient.parse_activity_json(fixture.read_text(encoding="utf-8"), "0xguru")
    store = StrategyStore()
    new = process_fixture_signals(sigs, store)

    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)
    strat = GuruFollowStrategy(app.strategy)

    async def _go() -> None:
        with JsonlSink(run_dir / "facts.jsonl") as sink:
            await process_new_guru_signals(
                new,
                app=app,
                run_id=RunId(str(uuid4())),
                strategy=strat,
                coord=coord,
                sink=sink,
                oms=ShadowOMS(),
            )

    asyncio.run(_go())

    summary = summarize_run(run_dir)
    assert summary["facts"] > 0
    ja = summary["join_audit"]
    assert ja["complete_approved_chains"] >= 1
    assert ja["strategy_skips"] >= 1

    ja2 = audit_fact_joins(run_dir)
    assert ja2["complete_approved_chains"] == ja["complete_approved_chains"]
