"""Force reduce-only exit for BOTH_LEGS_FILLED / BOTH_LEGS_ACTIVE."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.ids import RunId
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_DECISION_SNAPSHOT,
    FACT_TYPE_EXECUTION_PLANNER_EVIDENCE,
    FACT_TYPE_PAIRED_BINARY_SHUTDOWN_FORCE_FLATTEN_STARTED,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
from tyrex_pm.strategies.paired_binary.exit_engine import ensure_pnl_budgets
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase
from paired_binary_shutdown_helpers import (
    app_cfg,
    both_legs_active_state,
    coord_with_books,
    facts_from_sink,
    risk_cfg,
    seed_both_legs,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [PairedBinaryPhase.BOTH_LEGS_ACTIVE, PairedBinaryPhase.BOTH_LEGS_FILLED],
)
async def test_both_legs_phases_force_reduce_only_sells(tmp_path: Path, phase: PairedBinaryPhase) -> None:
    app = app_cfg(max_runtime_s=0.01, tick_interval_s=0.005)
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    seed_both_legs(coord)
    state = both_legs_active_state()
    state.phase = phase
    ensure_pnl_budgets(state, cfg)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        await run_paired_binary_loop(
            app=app,
            run_id=RunId(f"both-legs-{phase.value}"),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )
    facts = facts_from_sink(sink)
    types = {f["fact_type"] for f in facts}
    assert FACT_TYPE_PAIRED_BINARY_SHUTDOWN_FORCE_FLATTEN_STARTED in types
    sells = [f for f in facts if f["fact_type"] == "intent_created" and f["payload"].get("side") == "SELL"]
    assert len(sells) >= 2
    tokens = {f["payload"].get("token_id") for f in sells}
    assert cfg.yes_token_id in tokens
    assert cfg.no_token_id in tokens


@pytest.mark.asyncio
async def test_force_flatten_emits_decision_snapshot_and_planner_evidence(tmp_path: Path) -> None:
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
            run_id=RunId("obs-force"),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )
    types = {f["fact_type"] for f in facts_from_sink(sink)}
    assert FACT_TYPE_DECISION_SNAPSHOT in types
    assert FACT_TYPE_EXECUTION_PLANNER_EVIDENCE in types or "execution_plan" in types


@pytest.mark.asyncio
async def test_venue_min_size_block_leads_to_failed_not_done(tmp_path: Path) -> None:
    from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase

    app = app_cfg(
        max_runtime_s=0.01,
        tick_interval_s=0.005,
        risk=risk_cfg(
            venue_min_size={"enabled": True, "policy": "deny", "default_min_size": "5"},
        ),
    )
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    seed_both_legs(coord, qty=Decimal("1"))
    state = both_legs_active_state()
    state.effective_qty = Decimal("1")
    from tyrex_pm.strategies.paired_binary.exit_engine import ensure_pnl_budgets

    ensure_pnl_budgets(state, cfg)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        await run_paired_binary_loop(
            app=app,
            run_id=RunId("venue-block"),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )
    assert state.phase == PairedBinaryPhase.FAILED
    types = {f["fact_type"] for f in facts_from_sink(sink)}
    assert "paired_binary_manual_intervention_required" in types
    assert "paired_binary_shutdown_force_flatten_failed" in types
