"""Market-aware paired-binary state recovery identity tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from tyrex_pm.core.enums import ExecutionMode
from tyrex_pm.core.ids import RunId
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_PAIRED_BINARY_STATE_RECOVERY_APPLIED,
    FACT_TYPE_PAIRED_BINARY_STATE_RECOVERY_CHECKED,
    FACT_TYPE_PAIRED_BINARY_TERMINAL_STATE_RESET,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.paired_binary_recovery import recover_on_startup
from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.strategies.paired_binary.exit_engine import ensure_pnl_budgets
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from paired_binary_shutdown_helpers import (
    NO,
    YES,
    app_cfg,
    both_legs_active_state,
    coord_with_books,
    facts_from_sink,
    seed_both_legs,
    write_persisted_state,
)


def _recover(
    tmp_path: Path,
    *,
    allow_terminal_state_resume: bool = False,
) -> tuple[PairedBinaryRuntimeState, list[dict]]:
    app = app_cfg(allow_terminal_state_resume=allow_terminal_state_resume)
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        state = recover_on_startup(
            coord,
            cfg,
            state_dir=tmp_path,
            sink=sink,
            run_id=RunId("recovery-id"),
            pb_rt=app.runtime.paired_binary,
            execution_mode=ExecutionMode.SHADOW,
        )
        facts = facts_from_sink(sink)
    return state, facts


def test_persisted_done_same_tokens_resets_idle(tmp_path: Path) -> None:
    write_persisted_state(
        tmp_path,
        PairedBinaryRuntimeState(
            phase=PairedBinaryPhase.DONE,
            owner_id="paired_binary",
            market_id="m1",
            yes_token_id=YES,
            no_token_id=NO,
        ),
    )
    state, facts = _recover(tmp_path)
    assert state.phase == PairedBinaryPhase.IDLE
    types = {f["fact_type"] for f in facts}
    assert FACT_TYPE_PAIRED_BINARY_STATE_RECOVERY_CHECKED in types
    assert FACT_TYPE_PAIRED_BINARY_TERMINAL_STATE_RESET in types
    reset = next(f for f in facts if f["fact_type"] == FACT_TYPE_PAIRED_BINARY_TERMINAL_STATE_RESET)
    assert reset["payload"]["reason"] == "terminal_state_without_explicit_resume"


def test_persisted_both_legs_active_same_tokens_recovers(tmp_path: Path) -> None:
    persisted = both_legs_active_state()
    ensure_pnl_budgets(persisted, app_cfg().paired_binary)
    write_persisted_state(tmp_path, persisted)
    coord = coord_with_books(tmp_path)
    seed_both_legs(coord)
    app = app_cfg()
    cfg = app.paired_binary
    assert cfg is not None
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        state = recover_on_startup(
            coord,
            cfg,
            state_dir=tmp_path,
            sink=sink,
            run_id=RunId("recovery-active"),
            pb_rt=app.runtime.paired_binary,
        )
        facts = facts_from_sink(sink)
    assert state.phase == PairedBinaryPhase.BOTH_LEGS_ACTIVE
    applied = [f for f in facts if f["fact_type"] == FACT_TYPE_PAIRED_BINARY_STATE_RECOVERY_APPLIED]
    assert applied
    assert applied[0]["payload"]["reason"] == "valid_open_exposure_recovery"


def test_allow_terminal_state_resume_preserves_done(tmp_path: Path) -> None:
    write_persisted_state(
        tmp_path,
        PairedBinaryRuntimeState(
            phase=PairedBinaryPhase.DONE,
            owner_id="paired_binary",
            market_id="m1",
            yes_token_id=YES,
            no_token_id=NO,
        ),
    )
    state, facts = _recover(tmp_path, allow_terminal_state_resume=True)
    assert state.phase == PairedBinaryPhase.DONE
    types = {f["fact_type"] for f in facts}
    assert FACT_TYPE_PAIRED_BINARY_TERMINAL_STATE_RESET not in types


@pytest.mark.asyncio
async def test_new_run_after_done_gets_ticks_and_entry_eval(tmp_path: Path) -> None:
    write_persisted_state(
        tmp_path,
        PairedBinaryRuntimeState(
            phase=PairedBinaryPhase.DONE,
            owner_id="paired_binary",
            market_id="m1",
            yes_token_id=YES,
            no_token_id=NO,
        ),
    )
    app = app_cfg(max_runtime_s=0.05, tick_interval_s=0.005, entry_dry_run=False)
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        state = recover_on_startup(
            coord,
            cfg,
            state_dir=tmp_path,
            sink=sink,
            run_id=RunId("post-done-run"),
            pb_rt=app.runtime.paired_binary,
        )
        assert state.phase == PairedBinaryPhase.IDLE
        ticks = await run_paired_binary_loop(
            app=app,
            run_id=RunId("post-done-run"),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )
        facts = facts_from_sink(sink)
    assert ticks >= 1
    types = {f["fact_type"] for f in facts}
    assert "paired_binary_entry_eval" in types or "paired_binary_entry_skip" in types
    health = [f for f in facts if f.get("fact_type") == "health"]
    assert health[-1]["payload"]["event"] == "paired_binary_loop_stopped"
