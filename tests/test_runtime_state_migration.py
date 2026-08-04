"""Runtime-state migration: var/state → var/runtime_state."""

from __future__ import annotations

from pathlib import Path

import pytest

from tyrex_pm.runtime.r7_paths import (
    assert_live_runtime_state_ready,
    live_refuses_on_state_conflict,
    migrate_state_to_runtime_state,
)


def test_migrate_success_no_overwrite(tmp_path: Path):
    old = tmp_path / "state"
    new = tmp_path / "runtime_state"
    (old / "r7").mkdir(parents=True)
    ack = old / "r7" / "position_acknowledgment.json"
    ack.write_text('{"v":1}\n', encoding="utf-8")
    result = migrate_state_to_runtime_state(
        tmp_path, old_root=old, new_root=new, dry_run=False
    )
    assert result.ok
    assert "r7/position_acknowledgment.json" in result.migrated
    assert (new / "r7" / "position_acknowledgment.json").read_text(encoding="utf-8") == '{"v":1}\n'
    # second run: identical skip
    result2 = migrate_state_to_runtime_state(
        tmp_path, old_root=old, new_root=new, dry_run=False
    )
    assert result2.ok
    assert "r7/position_acknowledgment.json" in result2.skipped_identical
    assert not result2.refused_live


def test_migrate_conflict_refuses_live(tmp_path: Path):
    old = tmp_path / "state"
    new = tmp_path / "runtime_state"
    (old / "r7").mkdir(parents=True)
    (new / "r7").mkdir(parents=True)
    (old / "r7" / "position_acknowledgment.json").write_text('{"old":true}\n', encoding="utf-8")
    (new / "r7" / "position_acknowledgment.json").write_text('{"new":true}\n', encoding="utf-8")
    result = migrate_state_to_runtime_state(
        tmp_path, old_root=old, new_root=new, dry_run=False
    )
    assert not result.ok
    assert result.refused_live
    assert result.conflicts
    # Both preserved
    assert (old / "r7" / "position_acknowledgment.json").read_text(encoding="utf-8") == '{"old":true}\n'
    assert (new / "r7" / "position_acknowledgment.json").read_text(encoding="utf-8") == '{"new":true}\n'
    refuse, conflicts = live_refuses_on_state_conflict(
        repo_root=tmp_path, old_root=old, new_root=new
    )
    assert refuse and conflicts
    with pytest.raises(RuntimeError, match="LIVE refused"):
        assert_live_runtime_state_ready(tmp_path, old_root=old, new_root=new)
