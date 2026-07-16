"""Shutdown force-flatten must emit paired_binary_done like normal lifecycle DONE."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.ids import RunId
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_PAIRED_BINARY_DONE,
    FACT_TYPE_PAIRED_BINARY_OPEN_EXPOSURE_AT_SHUTDOWN,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
from tyrex_pm.strategies.paired_binary.exit_engine import ensure_pnl_budgets
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from paired_binary_shutdown_helpers import (
    YES,
    app_cfg,
    both_legs_active_state,
    coord_with_books,
    facts_from_sink,
    seed_both_legs,
)


@pytest.mark.asyncio
async def test_both_legs_active_max_runtime_emits_paired_binary_done(tmp_path: Path) -> None:
    app = app_cfg(max_runtime_s=0.01, tick_interval_s=0.005)
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    seed_both_legs(coord)
    state = both_legs_active_state()
    ensure_pnl_budgets(state, cfg)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        await run_paired_binary_loop(
            app=app,
            run_id=RunId("both-done-report"),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )
    facts = facts_from_sink(sink)
    types = {f["fact_type"] for f in facts}
    assert FACT_TYPE_PAIRED_BINARY_OPEN_EXPOSURE_AT_SHUTDOWN in types
    assert FACT_TYPE_PAIRED_BINARY_DONE in types
    done = next(f for f in facts if f["fact_type"] == FACT_TYPE_PAIRED_BINARY_DONE)
    assert done["payload"].get("completion_reason") == "shutdown_force_flatten"
    assert state.phase == PairedBinaryPhase.DONE


@pytest.mark.asyncio
async def test_only_yes_active_max_runtime_emits_paired_binary_done(tmp_path: Path) -> None:
    app = app_cfg(max_runtime_s=0.01, tick_interval_s=0.005)
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    coord.wallet.positions.clear()
    from tyrex_pm.core.ids import TokenId
    from tyrex_pm.core.models import WalletPosition

    NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"
    coord.wallet.positions[TokenId(YES)] = WalletPosition(
        token_id=TokenId(YES), qty=Decimal("5"), avg_price_usd=Decimal("0.50")
    )
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(YES), Decimal("5"), correlation_id="y")
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.ONLY_YES_ACTIVE,
        pair_correlation_id="pc_survivor_y",
        owner_id="paired_binary",
        market_id="m1",
        yes_token_id=YES,
        no_token_id=NO,
        yes_entry=Decimal("0.50"),
        no_entry=Decimal("0.51"),
        effective_qty=Decimal("5"),
        pair_cost=Decimal("1.01"),
    )
    ensure_pnl_budgets(state, cfg)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        await run_paired_binary_loop(
            app=app,
            run_id=RunId("only-yes-done-report"),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )
    types = {f["fact_type"] for f in facts_from_sink(sink)}
    assert FACT_TYPE_PAIRED_BINARY_DONE in types
    assert state.phase == PairedBinaryPhase.DONE
