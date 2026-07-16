"""Persisted lifecycle state must reflect final runtime state on exit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tyrex_pm.core.ids import RunId
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
from tyrex_pm.strategies.paired_binary.exit_engine import ensure_pnl_budgets
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, load_persisted_state
from paired_binary_shutdown_helpers import (
    app_cfg,
    both_legs_active_state,
    coord_with_books,
    persist_file,
    seed_both_legs,
)


@pytest.mark.asyncio
async def test_persisted_state_not_stale_entry_pending_on_both_legs_active_exit(tmp_path: Path) -> None:
    app = app_cfg(max_runtime_s=0.01, tick_interval_s=0.005)
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    seed_both_legs(coord)
    state = both_legs_active_state()
    ensure_pnl_budgets(state, cfg)
    path = persist_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "state": PairedBinaryPhase.BOTH_ENTRY_PENDING.value}),
        encoding="utf-8",
    )
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        await run_paired_binary_loop(
            app=app,
            run_id=RunId("persist-exit"),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )
    persisted = load_persisted_state(path)
    assert persisted is not None
    assert persisted.phase != PairedBinaryPhase.BOTH_ENTRY_PENDING
    assert persisted.phase in {
        PairedBinaryPhase.DONE,
        PairedBinaryPhase.FAILED,
        PairedBinaryPhase.BOTH_LEGS_ACTIVE,
    }
