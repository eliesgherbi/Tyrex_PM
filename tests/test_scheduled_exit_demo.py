"""Scheduled demo exit: shadow arms from instant fill; live arms from sellable inventory."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import ClientOrderId, RunId, TokenId
from tyrex_pm.core.models import ApprovedIntent, EnterIntent, WalletPosition
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.ingestion.guru_stream import process_fixture_signals
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_INTENT, FACT_TYPE_OMS_SUBMIT
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import load_app_config, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.pipeline import (
    process_new_guru_signals,
    process_scheduled_exit_demo_due,
)
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.strategy_store import StrategyStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.guru_follow.scheduled_exit_demo import (
    DEMO_EXIT_FACT_SOURCE,
    ScheduledExitDemoState,
)
from tyrex_pm.strategies.guru_follow.strategy import GuruFollowStrategy
from tyrex_pm.venue.polymarket.data_api_client import DataApiClient


def _app_with_demo(*, delay_s: float = 0.0):
    root = Path(__file__).resolve().parents[1]
    base = load_app_config(repo_root=root, scenario_file="shadow_guru")
    strat_raw = deepcopy(base.raw["strategy"])
    ex = strat_raw.setdefault("exits", {})
    ex["demo_forced_exit_enabled"] = True
    ex["demo_forced_exit_delay_s"] = delay_s
    app = parse_app_config(risk=base.raw["risk"], strategy=strat_raw, runtime=base.raw["runtime"])
    return app, root


async def _shadow_buy_then_drain_demo(tmp_path: Path, *, delay_s: float) -> list[dict]:
    app, root = _app_with_demo(delay_s=delay_s)
    assert app.runtime.shadow_bootstrap is not None
    run_id = RunId(str(uuid4()))
    store = StrategyStore()
    fixture = root / "tests" / "fixtures" / "data_api" / "activity_batch.json"
    sigs = DataApiClient.parse_activity_json(fixture.read_text(encoding="utf-8"), "0xguru")
    new = process_fixture_signals(sigs, store)
    strat = GuruFollowStrategy(app.strategy)
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / "demo_alloc.json")
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)
    facts_path = tmp_path / "facts.jsonl"
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
        await asyncio.sleep(float(delay_s) + 0.05)
        await process_scheduled_exit_demo_due(
            strategy=strat,
            app=app,
            run_id=run_id,
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            apply_local_shadow_fill=True,
            live_clob_client=None,
        )
    return [json.loads(ln) for ln in facts_path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_shadow_demo_exit_second_intent_has_provenance(tmp_path: Path) -> None:
    rows = asyncio.run(_shadow_buy_then_drain_demo(tmp_path, delay_s=0.02))
    intents = [r for r in rows if r["fact_type"] == FACT_TYPE_INTENT]
    assert len(intents) >= 2
    sell_row = next(r for r in intents if r["payload"].get("side") == "SELL")
    assert sell_row["payload"].get("source") == DEMO_EXIT_FACT_SOURCE
    assert sell_row["payload"].get("parent_correlation_id") == "act-a"
    assert "parent_buy_intent_id" in sell_row["payload"]
    assert "parent_client_order_id" in sell_row["payload"]

    submits = [r for r in rows if r["fact_type"] == FACT_TYPE_OMS_SUBMIT]
    assert len(submits) >= 2
    assert submits[-1]["payload"]["oms_result"] == "shadow_ack"


def test_live_demo_pending_arms_when_inventory_sufficient() -> None:
    from tyrex_pm.runtime.config import (
        ConvictionConfig,
        ExitsConfig,
        FiltersConfig,
        GuruConfig,
        SizingConfig,
        StrategyConfig,
    )

    cfg = StrategyConfig(
        guru=GuruConfig(
            wallet="0x",
            data_api_poll_interval_s=5,
            data_api_limit=50,
            data_api_max_pages_per_poll=5,
        ),
        filters=FiltersConfig(
            token_allowlist=frozenset(),
            min_notional_usd=Decimal("0"),
            significance_min_notional_usd=Decimal("0"),
            min_conviction_score=Decimal("-1e30"),
            exclude_untradeable_markets=False,
        ),
        sizing=SizingConfig(
            copy_scale=Decimal("1"),
            conviction=ConvictionConfig(
                enabled=False,
                score_min=Decimal("0"),
                score_max=Decimal("1"),
                min_multiplier=Decimal("1"),
                max_multiplier=Decimal("1"),
            ),
            static_enabled=False,
            static_amount_usd=Decimal("0"),
        ),
        exits=ExitsConfig(
            dust_notional_usd=Decimal("0.5"),
            sell_mode="proportional_to_guru",
            demo_forced_exit_enabled=True,
            demo_forced_exit_delay_s=3.0,
        ),
    )
    from tyrex_pm.runtime.allocation_ids import OWNER_GURU_FOLLOW

    demo = ScheduledExitDemoState(cfg.exits)
    tid = TokenId("tok1")
    ent = EnterIntent(
        token_id=tid,
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    ap = ApprovedIntent(intent=ent, client_order_id=ClientOrderId("cid-1"), run_id=RunId("r"))
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    ledger = AllocationLedger()
    coord.allocation_ledger = ledger
    ledger.apply_buy(OWNER_GURU_FOLLOW, tid, Decimal("10"), correlation_id="corr-1")
    demo.register_after_successful_buy(
        ap,
        coord,
        parent_correlation_id="corr-1",
        execution_mode=ExecutionMode.LIVE,
        apply_shadow_fill=False,
    )
    assert len(demo._pending_live) == 1
    assert not demo._armed

    demo.try_arm_live_pending(coord)
    assert len(demo._pending_live) == 1

    coord.wallet.positions[tid] = WalletPosition(token_id=tid, qty=Decimal("10"), avg_price_usd=Decimal("0.5"))
    demo.try_arm_live_pending(coord)
    assert len(demo._pending_live) == 0
    assert len(demo._armed) == 1
