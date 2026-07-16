"""Windows-safe atomic state persistence with retry and structured failure facts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tyrex_pm.core.ids import RunId
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.strategies.paired_binary import facts as pb_facts
from tyrex_pm.strategies.paired_binary.state import (
    PairedBinaryPhase,
    PairedBinaryRuntimeState,
    load_persisted_state,
    save_persisted_state,
    state_has_open_exposure,
)


def _idle_state() -> PairedBinaryRuntimeState:
    return PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.IDLE,
        market_id="m1",
        yes_token_id="yes",
        no_token_id="no",
        owner_id="paired_binary",
    )


def _open_exposure_state() -> PairedBinaryRuntimeState:
    return PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.BOTH_LEGS_ACTIVE,
        market_id="m1",
        yes_token_id="yes",
        no_token_id="no",
        owner_id="paired_binary",
    )


def test_save_persisted_state_succeeds_normally(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = _idle_state()
    result = save_persisted_state(path, state)
    assert result.success is True
    assert result.attempts >= 1
    loaded = load_persisted_state(path)
    assert loaded is not None
    assert loaded.phase == PairedBinaryPhase.IDLE


def test_save_persisted_state_retries_once_then_succeeds(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = _idle_state()
    original_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(5, "Access is denied", src, dst)
        return original_replace(src, dst)

    with patch("tyrex_pm.strategies.paired_binary.state.os.replace", side_effect=flaky_replace):
        result = save_persisted_state(path, state, max_attempts=5, retry_base_delay_s=0.001)

    assert result.success is True
    assert result.attempts == 2
    loaded = load_persisted_state(path)
    assert loaded is not None
    assert loaded.phase == PairedBinaryPhase.IDLE


def test_save_persisted_state_all_attempts_fail_keeps_previous_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "state": "DONE"}), encoding="utf-8")
    state = _idle_state()

    def always_denied(src, dst):
        raise PermissionError(5, "Access is denied", src, dst)

    with patch("tyrex_pm.strategies.paired_binary.state.os.replace", side_effect=always_denied):
        result = save_persisted_state(path, state, max_attempts=3, retry_base_delay_s=0.001)

    assert result.success is False
    assert result.attempts == 3
    assert result.error_type == "PermissionError"
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "DONE"
    tmp_files = list(path.parent.glob(f".{path.name}.*.tmp"))
    assert tmp_files == []


def test_save_persisted_state_failure_emits_structured_fact_and_idle_is_warning(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    state = _idle_state()

    with patch(
        "tyrex_pm.strategies.paired_binary.state.os.replace",
        side_effect=PermissionError(5, "Access is denied"),
    ):
        result = save_persisted_state(path, state, max_attempts=2, retry_base_delay_s=0.001)

    assert result.success is False
    assert result.severity == "warning"
    assert state_has_open_exposure(state) is False

    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        pb_facts.emit_state_persist_failed(sink, RunId("persist-fail"), state, result)
        rows = [json.loads(ln) for ln in sink._path.read_text(encoding="utf-8").splitlines()]

    payload = rows[0]["payload"]
    assert rows[0]["fact_type"] == "paired_binary_state_persist_failed"
    assert payload["path"] == str(path)
    assert payload["attempts"] == 2
    assert payload["severity"] == "warning"
    assert payload["has_open_exposure"] is False


def test_open_exposure_persist_failure_severity_is_error(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = _open_exposure_state()

    with patch(
        "tyrex_pm.strategies.paired_binary.state.os.replace",
        side_effect=PermissionError(5, "Access is denied"),
    ):
        result = save_persisted_state(path, state, max_attempts=2, retry_base_delay_s=0.001)

    assert result.success is False
    assert result.severity == "error"
    assert state_has_open_exposure(state) is True


@pytest.mark.asyncio
async def test_runtime_does_not_crash_on_idle_persist_failure(tmp_path: Path) -> None:
    from tyrex_pm.core.ids import RunId
    from tyrex_pm.execution.adapters import ShadowOMS
    from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
    from paired_binary_shutdown_helpers import app_cfg, coord_with_books, facts_from_sink

    app = app_cfg(max_runtime_s=0.02, entry_dry_run=True)
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    state = _idle_state()

    with patch(
        "tyrex_pm.strategies.paired_binary.state.os.replace",
        side_effect=PermissionError(5, "Access is denied"),
    ):
        with JsonlSink(tmp_path / "facts.jsonl") as sink:
            ticks = await run_paired_binary_loop(
                app=app,
                run_id=RunId("persist-no-crash"),
                coord=coord,
                sink=sink,
                oms=ShadowOMS(),
                cfg=cfg,
                state=state,
                state_dir=tmp_path,
            )
            facts = facts_from_sink(sink)

    assert ticks >= 1
    persist_facts = [f for f in facts if f.get("fact_type") == "paired_binary_state_persist_failed"]
    assert persist_facts
    assert persist_facts[-1]["payload"]["severity"] == "warning"
    health = [f for f in facts if f.get("fact_type") == "health"]
    assert health[-1]["payload"]["event"] == "paired_binary_loop_stopped"
