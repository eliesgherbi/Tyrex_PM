"""
T4 — Watermark durability: persisted guru store survives restart; overlapping replay
emits no duplicate accepted signals and produces no duplicate downstream intents.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import GuruTradeSignal
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.ingestion.guru_stream import process_fixture_signals
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_INTENT
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import load_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.pipeline import process_new_guru_signals
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.strategy_store import load_strategy_store, save_strategy_store
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.guru_follow.strategy import GuruFollowStrategy
from tyrex_pm.venue.polymarket.data_api_client import DataApiClient


def _fixture_text(root: Path) -> str:
    return (root / "tests" / "fixtures" / "data_api" / "activity_batch.json").read_text(encoding="utf-8")


def test_t4_persist_replay_same_fixture_produces_no_new_signals(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    state_path = tmp_path / "guru_state.json"
    text = _fixture_text(root)
    sigs = DataApiClient.parse_activity_json(text, "0xguru")

    store1 = load_strategy_store(state_path)
    new1 = process_fixture_signals(sigs, store1)
    assert len(new1) == 2
    save_strategy_store(state_path, store1)

    store2 = load_strategy_store(state_path)
    new2 = process_fixture_signals(sigs, store2)
    assert new2 == []

    act_c = GuruTradeSignal(
        guru_wallet="0xguru",
        token_id=TokenId("1234567890"),
        side=Side.BUY,
        size=Decimal("1"),
        price=Decimal("0.5"),
        notional_usd=Decimal("10"),
        dedup_key="act-c",
        ts_venue=datetime.fromtimestamp(1700000100, tz=timezone.utc),
    )
    store3 = load_strategy_store(state_path)
    new3 = process_fixture_signals(sigs + [act_c], store3)
    assert [s.dedup_key for s in new3] == ["act-c"]
    save_strategy_store(state_path, store3)


async def _run_pipeline_count_intents(
    *,
    new_sigs: list,
    facts_path: Path,
    run_id: RunId,
) -> int:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root, scenario_file="shadow_guru")
    assert app.runtime.shadow_bootstrap is not None
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)
    strat = GuruFollowStrategy(app.strategy)
    with JsonlSink(facts_path) as sink:
        await process_new_guru_signals(
            new_sigs,
            app=app,
            run_id=run_id,
            strategy=strat,
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
        )
    lines = [ln for ln in facts_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    intents = 0
    for ln in lines:
        row = json.loads(ln)
        if row.get("fact_type") == FACT_TYPE_INTENT:
            intents += 1
    return intents


def test_t4_restart_pipeline_no_duplicate_intents(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    state_path = tmp_path / "guru_state.json"
    text = _fixture_text(root)
    sigs = DataApiClient.parse_activity_json(text, "0xguru")

    store = load_strategy_store(state_path)
    new_a = process_fixture_signals(sigs, store)
    assert len(new_a) == 2
    save_strategy_store(state_path, store)

    f1 = tmp_path / "run1.jsonl"
    n_intent_1 = asyncio.run(
        _run_pipeline_count_intents(new_sigs=new_a, facts_path=f1, run_id=RunId(str(uuid4())))
    )
    assert n_intent_1 == 1

    store_b = load_strategy_store(state_path)
    new_b = process_fixture_signals(sigs, store_b)
    assert new_b == []

    f2 = tmp_path / "run2.jsonl"
    n_intent_2 = asyncio.run(
        _run_pipeline_count_intents(new_sigs=new_b, facts_path=f2, run_id=RunId(str(uuid4())))
    )
    assert n_intent_2 == 0
