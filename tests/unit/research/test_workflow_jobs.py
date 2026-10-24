"""Subprocess job ownership: lock, stop, crash, and resume."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from tyrex_pm.research.workflows.api import call
from tyrex_pm.research.workflows.jobs import stop_applies


def _save(root: Path, units: list[dict], experiment_id: str, pins: dict | None = None) -> None:
    saved = call(
        "experiment.save",
        {
            "experiment_id": experiment_id,
            "windows": [{"market_id": "m1", "collection": "c", "warehouse": "w"}],
            "parameters": {},
            "units": units,
            "pins": pins or {},
        },
        root=root,
    )
    assert saved["ok"] is True, saved


def _wait(root: Path, job_id: str, predicate, timeout: float = 25) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = call("job.status", {"job_id": job_id}, root=root)
        if last["ok"] and predicate(last):
            return last
        time.sleep(0.05)
    pytest.fail(f"timed out: {last}")


def _kill(pid: int) -> None:
    os.kill(pid, 15)


def test_stop_file_is_generation_scoped() -> None:
    assert stop_applies({"generation": 2}, 2)
    assert not stop_applies({"generation": 1}, 2)


def test_competing_start_and_stale_heartbeat_do_not_take_over(tmp_path: Path) -> None:
    root = tmp_path / "wf"
    _save(
        root,
        [{"id": "hold", "kind": "stale_heartbeat", "timeout_s": 20}],
        "exp-stale",
    )
    started = call("job.start", {"experiment_id": "exp-stale", "job_id": "stale"}, root=root)
    assert started["ok"] is True, started
    status = _wait(root, "stale", lambda item: item["result"]["heartbeat_at"] == 0)
    assert status["result"]["lock_held"] is True
    competing = call("job.start", {"experiment_id": "exp-stale", "job_id": "stale"}, root=root)
    assert competing["error"]["code"] == "job_busy"
    assert competing["error"]["fields"]["heartbeat_at"] == 0
    (root / "jobs" / "stale" / "proceed").write_text("go", encoding="utf-8")
    _wait(root, "stale", lambda item: item["result"]["state"] == "completed")


def test_parent_exit_stop_kill_and_resume_match_a_fresh_run(tmp_path: Path) -> None:
    root = tmp_path / "wf"
    units = [
        {"id": "first", "kind": "digest", "value": 7, "market_id": "m1"},
        {"id": "second", "kind": "gate", "value": 9, "market_id": "m1", "timeout_s": 20},
    ]
    _save(root, units, "exp-fresh")
    (root / "jobs").mkdir(parents=True, exist_ok=True)
    fresh_root = tmp_path / "fresh"
    _save(fresh_root, units, "exp-fresh")
    (fresh_root / "jobs").mkdir(parents=True, exist_ok=True)
    (fresh_root / "proceed-dir").mkdir()
    # Fresh run sees proceed before the gated unit.
    call("job.start", {"experiment_id": "exp-fresh", "job_id": "fresh"}, root=fresh_root)
    # The proceed file must exist in the job directory. Start creates that directory.
    deadline = time.time() + 10
    job_dir = fresh_root / "jobs" / "fresh"
    while time.time() < deadline and not job_dir.is_dir():
        time.sleep(0.02)
    (job_dir / "proceed").write_text("go", encoding="utf-8")
    _wait(fresh_root, "fresh", lambda item: item["result"]["state"] == "completed")

    _save(root, units, "exp-stop")
    started = call("job.start", {"experiment_id": "exp-stop", "job_id": "stopped"}, root=root)
    assert started["ok"] is True
    _wait(root, "stopped", lambda item: "first" in item["result"]["verified_units"])
    # The spawning call has already returned; the worker is not this process.
    assert started["result"]["pid"] not in {None, os.getpid()}
    stopped = call("job.stop", {"job_id": "stopped"}, root=root)
    assert stopped["result"]["stop_generation"] == 1
    interrupted = _wait(root, "stopped", lambda item: item["result"]["state"] == "interrupted")
    assert interrupted["result"]["lock_held"] is False
    assert interrupted["result"]["verified_units"] == ["first"]
    (root / "jobs" / "stopped" / "proceed").write_text("go", encoding="utf-8")
    resumed = call("job.resume", {"job_id": "stopped"}, root=root)
    assert resumed["ok"] is True, resumed
    _wait(root, "stopped", lambda item: item["result"]["state"] == "completed")
    fresh = call("results.open", {"job_id": "fresh"}, root=fresh_root)
    again = call("results.open", {"job_id": "stopped"}, root=root)
    assert fresh["result"]["windows"] == again["result"]["windows"]


def test_killed_worker_and_partial_publication_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "wf"
    fingerprint = tmp_path / "fingerprint.txt"
    fingerprint.write_text("fingerprint-a", encoding="utf-8")
    monkeypatch.setenv("TYREX_WORKFLOW_FINGERPRINT_FILE", str(fingerprint))
    pin = tmp_path / "pin.bin"
    pin.write_bytes(b"v1")
    from tyrex_pm.research.workflows.artifacts import file_sha256

    pins = {"pin_parts": [{"path": str(pin), "sha256": file_sha256(pin)}]}
    _save(root, [{"id": "hold", "kind": "gate", "value": 1, "timeout_s": 20}], "exp-kill", pins)
    started = call("job.start", {"experiment_id": "exp-kill", "job_id": "killed"}, root=root)
    assert started["ok"] is True, started
    status = _wait(root, "killed", lambda item: item["result"]["lock_held"] is True)
    os.kill(int(status["result"]["pid"]), 15)
    _wait(root, "killed", lambda item: item["result"]["lock_held"] is False)
    pin.write_bytes(b"v2")
    refused = call("job.resume", {"job_id": "killed"}, root=root)
    assert refused["error"]["code"] == "pin_mismatch"
    pin.write_bytes(b"v1")
    fingerprint.write_text("fingerprint-b", encoding="utf-8")
    changed = call("job.resume", {"job_id": "killed"}, root=root)
    assert changed["error"]["code"] == "identity_mismatch"
    assert "source_fingerprint" in changed["error"]["fields"]
    fingerprint.write_text("fingerprint-a", encoding="utf-8")

    crash_root = tmp_path / "crash"
    _save(
        crash_root, [{"id": "part", "kind": "digest", "value": 4, "market_id": "m1"}], "exp-crash"
    )
    job_dir = crash_root / "jobs" / "crashed"
    job_dir.mkdir(parents=True)
    (job_dir / "crash_once").write_text("part", encoding="utf-8")
    started_crash = call(
        "job.start", {"experiment_id": "exp-crash", "job_id": "crashed"}, root=crash_root
    )
    assert started_crash["ok"] is True, started_crash
    partial = job_dir / "parts" / "part.json.tmp"
    _wait(
        crash_root,
        "crashed",
        lambda item: (
            partial.is_file()
            and item["result"]["lock_held"] is False
            and item["result"]["state"] != "completed"
        ),
    )
    resumed = call("job.resume", {"job_id": "crashed"}, root=crash_root)
    assert resumed["ok"] is True, resumed
    done = _wait(crash_root, "crashed", lambda item: item["result"]["state"] == "completed")
    assert "part" in done["result"]["verified_units"]
    opened = call("results.open", {"job_id": "crashed"}, root=crash_root)
    row = opened["result"]["windows"][0]
    assert row["unit_id"] == "part"
    assert row["value"] == 4
    assert row["schema"] == "workflow_part_v1"
    assert row["source_fingerprint"] == "fingerprint-a"
