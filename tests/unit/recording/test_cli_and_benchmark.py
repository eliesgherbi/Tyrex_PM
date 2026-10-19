"""``tyrex-pm recording prune`` (dry run by default) and the recording benchmark."""

from __future__ import annotations

import os
import time
from pathlib import Path

from tyrex_pm.application.cli import main
from tyrex_pm.application.recording_cli import recordings_to_prune
from tyrex_pm.recording import Recorder, RecordingConfig
from tyrex_pm.recording.benchmark import run_benchmark


def _recording(folder: Path, age_days: float) -> Path:
    rec = Recorder(folder / "recording", RecordingConfig(), streams={"decisions": "t"},
                   manifest={})
    rec.start()
    rec.capture("decisions", {"a": 1})
    rec.close()
    old = time.time() - age_days * 86400
    for path in (folder / "recording").rglob("*.json"):
        os.utime(path, (old, old))
    return folder / "recording"


def test_prune_is_a_dry_run_and_only_removes_recorder_folders(tmp_path: Path, capsys) -> None:
    old = _recording(tmp_path / "run_old", 40)
    new = _recording(tmp_path / "run_new", 1)
    (tmp_path / "run_old" / "run_summary.json").write_text("{}")
    foreign = tmp_path / "other" / "recording"
    foreign.mkdir(parents=True)
    (foreign / "manifest.json").write_text('{"recorder": "something_else"}')
    os.utime(foreign / "manifest.json", (0, 0))
    assert [f for f, _, _ in recordings_to_prune(tmp_path, 30)] == [old]
    assert main(["recording", "prune", "--root", str(tmp_path)]) == 0
    assert "would delete" in capsys.readouterr().out and old.exists()
    assert main(["recording", "prune", "--root", str(tmp_path), "--apply"]) == 0
    assert not old.exists() and new.exists() and foreign.exists()
    assert (tmp_path / "run_old" / "run_summary.json").exists()


def test_benchmark_reports_cost_memory_and_disk_failures(tmp_path: Path) -> None:
    result = run_benchmark(out=tmp_path, records=400, slow_disk_delay_s=0.001)
    cases = {c["case"]: c for c in result["cases"]}
    assert cases["normal"]["written"] == 800 and cases["normal"]["dropped"] == 0
    assert cases["memory"]["peak_traced_memory_mib"] is not None
    assert cases["full_disk"]["failed"] and cases["full_disk"]["errors"]
    assert cases["slow_disk"]["dropped"] + cases["slow_disk"]["written"] == 800
    assert (tmp_path / "benchmark.json").exists()
