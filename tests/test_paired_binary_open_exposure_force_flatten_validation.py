"""Integration proof: BOTH_LEGS_ACTIVE + max_runtime triggers open-exposure force-flatten."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.ids import RunId
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_PAIRED_BINARY_OPEN_EXPOSURE_AT_SHUTDOWN,
    FACT_TYPE_PAIRED_BINARY_SHUTDOWN_FORCE_FLATTEN_DONE,
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
    seed_both_legs,
)


@pytest.mark.asyncio
async def test_both_legs_active_max_runtime_force_flatten_validation(tmp_path: Path) -> None:
    """Option A fixture proof — live proof remains optional/pending."""
    app = app_cfg(max_runtime_s=0.01, tick_interval_s=0.005)
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    seed_both_legs(coord)
    state = both_legs_active_state()
    ensure_pnl_budgets(state, cfg)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        ticks = await run_paired_binary_loop(
            app=app,
            run_id=RunId("force-flatten-validation"),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )
        facts = facts_from_sink(sink)
    types = {f["fact_type"] for f in facts}
    assert ticks >= 1
    assert state.phase == PairedBinaryPhase.BOTH_LEGS_ACTIVE or state.phase in {
        PairedBinaryPhase.DONE,
        PairedBinaryPhase.FAILED,
        PairedBinaryPhase.EXITING_YES,
        PairedBinaryPhase.EXITING_NO,
        PairedBinaryPhase.EXITING_BOTH,
    }
    assert FACT_TYPE_PAIRED_BINARY_OPEN_EXPOSURE_AT_SHUTDOWN in types
    assert FACT_TYPE_PAIRED_BINARY_SHUTDOWN_FORCE_FLATTEN_STARTED in types
    assert FACT_TYPE_PAIRED_BINARY_SHUTDOWN_FORCE_FLATTEN_DONE in types or state.phase in {
        PairedBinaryPhase.DONE,
        PairedBinaryPhase.FAILED,
    }
    assert "paired_binary_done" in types
    assert (
        "paired_binary_realized_pnl" in types
        or "paired_binary_realized_pnl_tentative" in types
        or "paired_binary_realized_pnl_unavailable" in types
    )
    sell_intents = [
        f
        for f in facts
        if f["fact_type"] == "intent_created" and f["payload"].get("side") == "SELL"
    ]
    assert len(sell_intents) >= 2
    oms_submits = [f for f in facts if f["fact_type"] == "oms_submit"]
    assert not any(f["payload"].get("source") == "rest" for f in oms_submits)
    exposure = next(
        f for f in facts if f["fact_type"] == FACT_TYPE_PAIRED_BINARY_OPEN_EXPOSURE_AT_SHUTDOWN
    )
    assert exposure["payload"]["state"] == PairedBinaryPhase.BOTH_LEGS_ACTIVE.value
    health = [f for f in facts if f.get("fact_type") == "health"]
    assert health[-1]["payload"]["event"] == "paired_binary_loop_stopped"
