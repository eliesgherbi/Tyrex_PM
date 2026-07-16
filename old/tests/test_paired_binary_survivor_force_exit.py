"""Force survivor exit on max_runtime tests."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import WalletPosition
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_DECISION_SNAPSHOT,
    FACT_TYPE_EXECUTION_PLANNER_EVIDENCE,
    FACT_TYPE_PAIRED_BINARY_SHUTDOWN_FORCE_FLATTEN_STARTED,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import OPEN_EXPOSURE_ON_MAX_RUNTIME_FORCE, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.paired_binary_run import _unwind_leg_for_shutdown
from tyrex_pm.runtime.paired_binary_shutdown import handle_open_exposure_at_shutdown
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore, make_snapshot
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.entry_eval import read_leg_book
from tyrex_pm.strategies.paired_binary.exit_engine import ensure_pnl_budgets
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from tyrex_pm.strategies.paired_binary.strategy import PairedBinaryStrategy

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"


def _setup(tmp_path: Path):
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
                "exit_order_style": "FAK",
            },
        },
        runtime={
            "execution_mode": "shadow",
            "reporting": {"enabled": True},
            "market_data": {"enabled": True, "max_book_age_s": 5},
            "execution": {"planner": {"enabled": True}},
            "observability": {"emit_decision_snapshot": True},
            "paired_binary": {"open_exposure_on_max_runtime": OPEN_EXPOSURE_ON_MAX_RUNTIME_FORCE},
        },
    )
    cfg = app.paired_binary
    assert cfg is not None
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    store = MarketStateStore(default_max_age_s=5.0)
    store.apply_snapshot(
        make_snapshot(TokenId(YES), bids=[(Decimal("0.43"), Decimal("100"))], asks=[(Decimal("0.46"), Decimal("100"))])
    )
    store.apply_snapshot(
        make_snapshot(TokenId(NO), bids=[(Decimal("0.54"), Decimal("100"))], asks=[(Decimal("0.57"), Decimal("100"))])
    )
    coord.market_state = store
    coord.wallet.positions[TokenId(NO)] = WalletPosition(
        token_id=TokenId(NO), qty=Decimal("5"), avg_price_usd=Decimal("0.47")
    )
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(NO), Decimal("5"), correlation_id="n1")
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.ONLY_NO_ACTIVE,
        pair_correlation_id="pc1",
        owner_id="paired_binary",
        market_id="m1",
        yes_token_id=YES,
        no_token_id=NO,
        no_entry=Decimal("0.47"),
        effective_qty=Decimal("5"),
    )
    ensure_pnl_budgets(state, cfg)
    yes_book = read_leg_book(store, TokenId(YES), max_book_age_s=5.0)
    no_book = read_leg_book(store, TokenId(NO), max_book_age_s=5.0)
    assert yes_book is not None and no_book is not None
    return app, cfg, coord, state, yes_book, no_book


@pytest.mark.asyncio
async def test_force_exit_submits_survivor_sell(tmp_path: Path) -> None:
    app, cfg, coord, state, yes_book, no_book = _setup(tmp_path)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        oms = ShadowOMS()
        strategy = PairedBinaryStrategy(cfg)
        strategy.bind_state(state)
        await handle_open_exposure_at_shutdown(
            app=app,
            run_id=RunId("force-exit"),
            coord=coord,
            sink=sink,
            oms=oms,
            strategy=strategy,
            cfg=cfg,
            state=state,
            yes_book=yes_book,
            no_book=no_book,
            apply_local_shadow_fill=True,
            live_clob_client=None,
            unwind_leg_fn=_unwind_leg_for_shutdown,
        )
        facts_path = sink._path
    assert state.phase == PairedBinaryPhase.DONE
    facts = [json.loads(ln) for ln in facts_path.read_text(encoding="utf-8").splitlines()]
    types = {f["fact_type"] for f in facts}
    assert FACT_TYPE_PAIRED_BINARY_SHUTDOWN_FORCE_FLATTEN_STARTED in types
    assert "oms_submit" in types


@pytest.mark.asyncio
async def test_force_exit_emits_decision_snapshot_when_enabled(tmp_path: Path) -> None:
    app, cfg, coord, state, yes_book, no_book = _setup(tmp_path)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        oms = ShadowOMS()
        strategy = PairedBinaryStrategy(cfg)
        strategy.bind_state(state)
        await handle_open_exposure_at_shutdown(
            app=app,
            run_id=RunId("force-exit-obs"),
            coord=coord,
            sink=sink,
            oms=oms,
            strategy=strategy,
            cfg=cfg,
            state=state,
            yes_book=yes_book,
            no_book=no_book,
            apply_local_shadow_fill=True,
            live_clob_client=None,
            unwind_leg_fn=_unwind_leg_for_shutdown,
        )
        facts_path = sink._path
    facts = [json.loads(ln) for ln in facts_path.read_text(encoding="utf-8").splitlines()]
    types = {f["fact_type"] for f in facts}
    assert FACT_TYPE_DECISION_SNAPSHOT in types
    assert FACT_TYPE_EXECUTION_PLANNER_EVIDENCE in types or "execution_plan" in types
