"""Deleting sets and results moves them to a recoverable trash; live work is protected."""

from __future__ import annotations

from pathlib import Path

from tyrex_pm.research.workflows.api import call
from tyrex_pm.research.workflows.artifacts import write_json


def _set(root: Path, selection_id: str = "set1", policy: str = "replay_research_v2_1") -> None:
    write_json(
        root / "selections" / f"{selection_id}.json",
        {
            "selection_id": selection_id,
            "name": "My set",
            "recipe": "eligible_windows",
            "research_policy": policy,
            "included": [{"market_id": "m1"}],
        },
    )


def _run(root: Path, state: str, experiment_id: str = "exp1", batch_id: str = "b1") -> None:
    write_json(
        root / "experiments" / f"{experiment_id}.json",
        {
            "experiment_id": experiment_id,
            "batch_id": batch_id,
            "selection_id": "set1",
            "scenario_name": "friction_v3",
            "configurations": [{"name": "Edge 3c"}],
        },
    )
    write_json(
        root / "replay_batches" / f"{batch_id}.json",
        {"schema": "replay_batch_v1", "batch_id": batch_id, "experiments": [experiment_id]},
    )
    job = root / "jobs" / f"job-{experiment_id}"
    write_json(job / "job.json", {"job_id": f"job-{experiment_id}", "experiment_id": experiment_id})
    write_json(job / "status.json", {"state": state})


def test_a_deleted_set_goes_to_trash_and_can_be_restored(tmp_path: Path) -> None:
    _set(tmp_path)
    deleted = call("selection.delete", {"selection_id": "set1"}, root=tmp_path)
    assert deleted["ok"], deleted
    assert not (tmp_path / "selections" / "set1.json").exists()
    listed = call("trash.list", {}, root=tmp_path)["result"]
    assert listed["entries"][0]["kind"] == "window_set"
    assert listed["entries"][0]["label"] == "My set"
    restored = call("trash.restore", {"entry_id": deleted["result"]["entry_id"]}, root=tmp_path)
    assert restored["ok"], restored
    assert (tmp_path / "selections" / "set1.json").is_file()
    assert call("trash.list", {}, root=tmp_path)["result"]["total"] == 0


def test_a_set_used_by_a_running_replay_cannot_be_deleted(tmp_path: Path) -> None:
    _set(tmp_path)
    _run(tmp_path, "running")
    refused = call("selection.delete", {"selection_id": "set1"}, root=tmp_path)
    assert refused["ok"] is False
    assert refused["error"]["code"] == "set_in_use"


def test_deleting_a_run_takes_its_jobs_and_its_now_empty_batch(tmp_path: Path) -> None:
    _set(tmp_path)
    _run(tmp_path, "completed")
    deleted = call("replay.delete", {"experiment_id": "exp1"}, root=tmp_path)
    assert deleted["ok"], deleted
    assert not (tmp_path / "experiments" / "exp1.json").exists()
    assert not (tmp_path / "jobs" / "job-exp1").exists()
    assert not (tmp_path / "replay_batches" / "b1.json").exists()
    history = call("replay.history", {}, root=tmp_path)["result"]
    assert history["total"] == 0
    call("trash.restore", {"entry_id": deleted["result"]["entry_id"]}, root=tmp_path)
    assert call("replay.history", {}, root=tmp_path)["result"]["total"] == 1


def test_a_running_replay_cannot_be_deleted(tmp_path: Path) -> None:
    _set(tmp_path)
    _run(tmp_path, "running")
    refused = call("replay.delete", {"experiment_id": "exp1"}, root=tmp_path)
    assert refused["error"]["code"] == "replay_running"


def test_purge_needs_an_explicit_confirmation(tmp_path: Path) -> None:
    _set(tmp_path)
    call("selection.delete", {"selection_id": "set1"}, root=tmp_path)
    assert call("trash.purge", {}, root=tmp_path)["error"]["code"] == "confirmation_required"
    purged = call("trash.purge", {"confirm": "delete permanently"}, root=tmp_path)
    assert purged["result"]["purged"] == 1
    assert not any((tmp_path / "trash").iterdir())


def test_listings_say_which_items_match_the_current_rules(tmp_path: Path) -> None:
    _set(tmp_path, "set1", "replay_research_v2_1")
    _set(tmp_path, "old", "exploratory")
    _run(tmp_path, "completed")
    sets = {
        row["selection_id"]: row["status"]
        for row in call("selection.list", {}, root=tmp_path)["result"]["selections"]
    }
    assert sets == {"set1": "current", "old": "legacy_policy"}
    run = call("replay.history", {}, root=tmp_path)["result"]["runs"][0]
    assert run["status"] == "current"
    call("selection.delete", {"selection_id": "set1"}, root=tmp_path)
    run = call("replay.history", {}, root=tmp_path)["result"]["runs"][0]
    assert run["status"] == "set_deleted"
