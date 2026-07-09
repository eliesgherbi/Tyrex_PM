"""Terminal persisted lifecycle must not block new runs (default reset to IDLE)."""

from __future__ import annotations

from pathlib import Path

from tyrex_pm.core.ids import RunId
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_PAIRED_BINARY_TERMINAL_STATE_RESET
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.paired_binary_recovery import recover_on_startup
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from paired_binary_shutdown_helpers import (
    NO,
    YES,
    app_cfg,
    coord_with_books,
    facts_from_sink,
    write_persisted_state,
)

ALT_YES = YES[:-1] + "1"
ALT_NO = NO[:-1] + "2"


def test_persisted_failed_new_tokens_resets_idle(tmp_path: Path) -> None:
    write_persisted_state(
        tmp_path,
        PairedBinaryRuntimeState(
            phase=PairedBinaryPhase.FAILED,
            owner_id="paired_binary",
            market_id="m1",
            yes_token_id=YES,
            no_token_id=NO,
        ),
    )
    app = app_cfg(yes_token_id=ALT_YES, no_token_id=ALT_NO)
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        state = recover_on_startup(
            coord,
            cfg,
            state_dir=tmp_path,
            sink=sink,
            run_id=RunId("failed-new-tokens"),
            pb_rt=app.runtime.paired_binary,
        )
        facts = facts_from_sink(sink)
    assert state.phase == PairedBinaryPhase.IDLE
    assert "paired_binary_state_recovery_ignored" in {f["fact_type"] for f in facts}


def test_persisted_done_same_tokens_emits_terminal_reset(tmp_path: Path) -> None:
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
    app = app_cfg()
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        state = recover_on_startup(
            coord,
            cfg,
            state_dir=tmp_path,
            sink=sink,
            run_id=RunId("done-reset"),
            pb_rt=app.runtime.paired_binary,
        )
        facts = facts_from_sink(sink)
    assert state.phase == PairedBinaryPhase.IDLE
    reset_facts = [f for f in facts if f["fact_type"] == FACT_TYPE_PAIRED_BINARY_TERMINAL_STATE_RESET]
    assert reset_facts
    assert reset_facts[0]["payload"]["reason"] == "terminal_state_without_explicit_resume"
    assert reset_facts[0]["payload"]["terminal_state"] == PairedBinaryPhase.DONE.value


def test_allow_terminal_resume_keeps_done_on_same_pair(tmp_path: Path) -> None:
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
    app = app_cfg(allow_terminal_state_resume=True)
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        state = recover_on_startup(
            coord,
            cfg,
            state_dir=tmp_path,
            sink=sink,
            run_id=RunId("done-resume"),
            pb_rt=app.runtime.paired_binary,
        )
    assert state.phase == PairedBinaryPhase.DONE
