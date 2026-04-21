"""Phase 6: ``tyrex-pm reset-state`` CLI + ``reset_local_state`` helper.

Verifies the V2 cutover hygiene:
- documented state files are deleted from ``state_dir`` (idempotent on a clean tree),
- reporting artifacts under ``var/reporting/`` are NEVER touched,
- the CLI command runs without error and prints what it removed,
- non-existent or non-directory ``state_dir`` is handled correctly.

See Docs/Implementation/V2_migration_plan.md §6.3 + §7 Phase 6.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tyrex_pm.runtime.reset_state import reset_local_state, resettable_file_names


def test_reset_local_state_removes_documented_files(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    target = state_dir / "guru_strategy_store.json"
    target.write_text(
        json.dumps({"version": 1, "guru_watermark": None, "guru_seen_dedup": []}),
        encoding="utf-8",
    )

    removed = reset_local_state(state_dir)

    assert removed == [target]
    assert not target.exists()


def test_reset_local_state_is_idempotent(tmp_path: Path) -> None:
    """Running the reset twice on a clean tree must be a no-op the second time."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "guru_strategy_store.json").write_text("{}", encoding="utf-8")

    first = reset_local_state(state_dir)
    second = reset_local_state(state_dir)

    assert len(first) == 1
    assert second == []


def test_reset_local_state_empty_when_state_dir_missing(tmp_path: Path) -> None:
    """A missing state_dir is fine — that's the cleanest possible posture."""
    state_dir = tmp_path / "does-not-exist"
    assert reset_local_state(state_dir) == []


def test_reset_local_state_raises_when_state_dir_is_a_file(tmp_path: Path) -> None:
    """Passing a regular file as ``state_dir`` is an operator mistake; surface it."""
    p = tmp_path / "state"
    p.write_text("not a dir", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        reset_local_state(p)


def test_reset_local_state_does_not_touch_reporting_or_unrelated_files(tmp_path: Path) -> None:
    """Reporting history is immutable — and so is anything outside the documented file list."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "guru_strategy_store.json").write_text("{}", encoding="utf-8")
    # Defensive: a sibling file that is NOT in the documented list must survive.
    sibling = state_dir / "operator_notes.md"
    sibling.write_text("keep me", encoding="utf-8")
    # Reporting tree under the same parent: must survive untouched.
    reporting = tmp_path / "reporting" / "runs" / "abc"
    reporting.mkdir(parents=True)
    (reporting / "facts.jsonl").write_text("{}\n", encoding="utf-8")

    reset_local_state(state_dir)

    assert sibling.exists()
    assert (reporting / "facts.jsonl").exists()


def test_resettable_file_names_includes_guru_store() -> None:
    """Pin the documented file list so adding a new state file requires updating both sides."""
    names = resettable_file_names()
    assert "guru_strategy_store.json" in names
    assert all(isinstance(n, str) for n in names)


# ---------------------------------------------------------------------------
# CLI surface (``python -m tyrex_pm.runtime.app reset-state ...``).
# ---------------------------------------------------------------------------


def test_cli_reset_state_runs_and_prints_removed_path(tmp_path: Path) -> None:
    """End-to-end: invoke the CLI and check the documented file is gone."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    target = state_dir / "guru_strategy_store.json"
    target.write_text("{}", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tyrex_pm.runtime.app",
            "reset-state",
            "--state-dir",
            str(state_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "removed" in result.stdout
    assert "guru_strategy_store.json" in result.stdout
    assert not target.exists()


def test_cli_reset_state_idempotent_says_nothing_to_clear(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tyrex_pm.runtime.app",
            "reset-state",
            "--state-dir",
            str(state_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "no state to clear" in result.stdout
