"""Open-exposure shutdown policy integration tests."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.ids import RunId
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_PAIRED_BINARY_OPEN_EXPOSURE_AT_SHUTDOWN
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
from tyrex_pm.strategies.paired_binary.exit_engine import ensure_pnl_budgets
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from paired_binary_shutdown_helpers import (
    app_cfg,
    both_legs_active_state,
    coord_with_books,
    facts_from_sink,
    only_no_state,
    seed_both_legs,
    seed_no_leg,
)


@pytest.mark.asyncio
async def test_idle_max_runtime_stops_normally(tmp_path: Path) -> None:
    app = app_cfg(max_runtime_s=0.02, entry_dry_run=True)
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    state = PairedBinaryRuntimeState(phase=PairedBinaryPhase.IDLE)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        await run_paired_binary_loop(
            app=app,
            run_id=RunId("idle-stop"),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )
    assert state.phase == PairedBinaryPhase.IDLE
    types = {f["fact_type"] for f in facts_from_sink(sink)}
    assert FACT_TYPE_PAIRED_BINARY_OPEN_EXPOSURE_AT_SHUTDOWN not in types


@pytest.mark.asyncio
async def test_live4_regression_both_legs_active_force_flatten(tmp_path: Path) -> None:
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
            run_id=RunId("live4-regression"),
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
    assert "paired_binary_shutdown_force_flatten_started" in types
    assert any(
        f["fact_type"] == "intent_created" and f["payload"].get("side") == "SELL" for f in facts
    )
    assert state.phase != PairedBinaryPhase.BOTH_LEGS_ACTIVE
    health = [
        f
        for f in facts
        if f.get("fact_type") == "health" and f["payload"].get("event") == "paired_binary_loop_stopped"
    ]
    assert health[-1]["payload"]["final_state"] != PairedBinaryPhase.BOTH_LEGS_ACTIVE.value


@pytest.mark.asyncio
async def test_only_no_active_survivor_still_force_exits(tmp_path: Path) -> None:
    app = app_cfg(max_runtime_s=0.01, tick_interval_s=0.005)
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    seed_no_leg(coord)
    state = only_no_state()
    ensure_pnl_budgets(state, cfg)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        await run_paired_binary_loop(
            app=app,
            run_id=RunId("survivor-force"),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )
    assert state.phase != PairedBinaryPhase.ONLY_NO_ACTIVE
    types = {f["fact_type"] for f in facts_from_sink(sink)}
    assert FACT_TYPE_PAIRED_BINARY_OPEN_EXPOSURE_AT_SHUTDOWN in types
