"""Loop health facts on normal, error, and interrupted exit."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tyrex_pm.core.ids import RunId
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from paired_binary_shutdown_helpers import app_cfg, coord_with_books, facts_from_sink


@pytest.mark.asyncio
async def test_loop_emits_stopped_on_normal_exit(tmp_path: Path) -> None:
    app = app_cfg(max_runtime_s=0.02, entry_dry_run=True)
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    state = PairedBinaryRuntimeState(phase=PairedBinaryPhase.IDLE)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        await run_paired_binary_loop(
            app=app,
            run_id=RunId("loop-stopped"),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )
        facts = facts_from_sink(sink)
    health = [f for f in facts if f.get("fact_type") == "health"]
    assert health[-1]["payload"]["event"] == "paired_binary_loop_stopped"


@pytest.mark.asyncio
async def test_loop_emits_failed_on_tick_exception(tmp_path: Path) -> None:
    app = app_cfg(max_runtime_s=0.05, entry_dry_run=True)
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    state = PairedBinaryRuntimeState(phase=PairedBinaryPhase.IDLE)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        with patch(
            "tyrex_pm.runtime.paired_binary_run._paired_binary_tick_body",
            new=AsyncMock(side_effect=RuntimeError("tick boom")),
        ):
            with pytest.raises(RuntimeError, match="tick boom"):
                await run_paired_binary_loop(
                    app=app,
                    run_id=RunId("loop-failed"),
                    coord=coord,
                    sink=sink,
                    oms=ShadowOMS(),
                    cfg=cfg,
                    state=state,
                    state_dir=tmp_path,
                )
        facts = facts_from_sink(sink)
    health = [f for f in facts if f.get("fact_type") == "health"]
    assert health[-1]["payload"]["event"] == "paired_binary_loop_failed"
    assert "tick boom" in health[-1]["payload"]["error"]
