"""Delete window sets and replay results into a recoverable trash.

Deleting moves the saved files under ``<root>/trash/<entry_id>/`` with a manifest of where
each came from. ``trash.restore`` moves them back; ``trash.purge`` is the only operation
that removes bytes. Anything a live job still uses is refused.
"""

from __future__ import annotations

import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .artifacts import now_iso, read, write_json
from .contracts import WorkflowError

LIVE = {"preparing", "running"}
CURRENT_POLICIES = {"replay_research_v2_1"}
PREVIOUS_POLICIES = {"replay_research_v2", "exploratory"}
CURRENT_SCENARIOS = {"friction_v3", "friction_stress_v2"}


def _jobs(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in (root / "jobs").glob("*/job.json") if (root / "jobs").is_dir() else []:
        body = read(path)
        status_path = path.parent / "status.json"
        status = read(status_path) if status_path.is_file() else {}
        rows.append(
            {
                "job_id": body.get("job_id"),
                "experiment_id": body.get("experiment_id"),
                "dir": path.parent,
                "state": status.get("state"),
            }
        )
    return rows


def _experiments_using(root: Path, selection_id: str) -> list[str]:
    found = []
    for path in (root / "experiments").glob("*.json") if (root / "experiments").is_dir() else []:
        if read(path).get("selection_id") == selection_id:
            found.append(path.stem)
    return found


def _move(root: Path, entry: Path, source: Path, moved: list[dict]) -> None:
    relative = source.relative_to(root)
    target = entry / "files" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    moved.append({"path": str(relative).replace("\\", "/")})


def _entry(
    entry_id: str, kind: str, label: str, subject: str, moved: list[dict], extra: dict
) -> dict:
    return {
        "schema": "workflow_trash_entry_v1",
        "entry_id": entry_id,
        "kind": kind,
        "subject_id": subject,
        "label": label,
        "deleted_at": now_iso(),
        "files": moved,
        **extra,
    }


def selection_delete(request: dict, root: Path) -> dict[str, Any]:
    selection_id = str(request.get("selection_id") or "")
    path = root / "selections" / f"{selection_id}.json"
    if not selection_id or not path.is_file():
        raise WorkflowError("not_found", "window set is not saved", {"selection_id": selection_id})
    saved = read(path)
    experiments = set(_experiments_using(root, selection_id))
    live = [
        job["job_id"]
        for job in _jobs(root)
        if job["experiment_id"] in experiments and job["state"] in LIVE
    ]
    if live:
        raise WorkflowError(
            "set_in_use", "a running replay still reads this window set", {"job_ids": live}
        )
    entry_id = uuid.uuid4().hex
    entry = root / "trash" / entry_id
    moved: list[dict] = []
    _move(root, entry, path, moved)
    body = _entry(
        entry_id,
        "window_set",
        str(saved.get("name") or selection_id),
        selection_id,
        moved,
        {"runs_still_referencing": sorted(experiments)},
    )
    write_json(entry / "entry.json", body)
    return {
        "entry_id": entry_id,
        "deleted": selection_id,
        "runs_still_referencing": len(experiments),
    }


def replay_delete(request: dict, root: Path) -> dict[str, Any]:
    """Delete one run: its experiment, every job of it, and the batch once it is empty."""
    experiment_id = str(request.get("experiment_id") or "")
    path = root / "experiments" / f"{experiment_id}.json"
    if not experiment_id or not path.is_file():
        raise WorkflowError("not_found", "replay is not saved", {"experiment_id": experiment_id})
    experiment = read(path)
    jobs = [job for job in _jobs(root) if job["experiment_id"] == experiment_id]
    live = [job["job_id"] for job in jobs if job["state"] in LIVE]
    if live:
        raise WorkflowError(
            "replay_running", "stop the replay before deleting it", {"job_ids": live}
        )
    entry_id = uuid.uuid4().hex
    entry = root / "trash" / entry_id
    moved: list[dict] = []
    for job in jobs:
        _move(root, entry, job["dir"], moved)
    _move(root, entry, path, moved)
    batch_id = experiment.get("batch_id")
    batch_path = root / "replay_batches" / f"{batch_id}.json" if batch_id else None
    if batch_path and batch_path.is_file():
        siblings = [
            item
            for item in read(batch_path).get("experiments") or []
            if (root / "experiments" / f"{item}.json").is_file()
        ]
        if not siblings:
            _move(root, entry, batch_path, moved)
            folder = root / "replay_batches" / str(batch_id)
            if folder.is_dir():
                _move(root, entry, folder, moved)
    names = [item.get("name") for item in experiment.get("configurations") or []]
    body = _entry(
        entry_id,
        "replay_run" if jobs else "replay_preview",
        " · ".join(str(name) for name in names if name) or experiment_id,
        experiment_id,
        moved,
        {
            "job_ids": [job["job_id"] for job in jobs],
            "selection_id": experiment.get("selection_id"),
        },
    )
    write_json(entry / "entry.json", body)
    return {"entry_id": entry_id, "deleted": experiment_id, "jobs": len(jobs)}


def trash_list(request: dict, root: Path) -> dict[str, Any]:
    del request
    rows = []
    folder = root / "trash"
    now = time.time()
    for path in sorted(folder.glob("*/entry.json")) if folder.is_dir() else []:
        body = read(path)
        files = path.parent / "files"
        body["size_bytes"] = (
            sum(item.stat().st_size for item in files.rglob("*") if item.is_file())
            if files.is_dir()
            else 0
        )
        deleted = datetime.fromisoformat(str(body.get("deleted_at")))
        body["age_days"] = round((now - deleted.timestamp()) / 86400, 1)
        rows.append(body)
    rows.sort(key=lambda item: str(item.get("deleted_at") or ""), reverse=True)
    return {"entries": rows, "total": len(rows)}


def _entry_dir(root: Path, entry_id: str) -> Path:
    entry = root / "trash" / entry_id
    if not entry_id or not (entry / "entry.json").is_file():
        raise WorkflowError("not_found", "trash entry is not saved", {"entry_id": entry_id})
    return entry


def trash_restore(request: dict, root: Path) -> dict[str, Any]:
    entry = _entry_dir(root, str(request.get("entry_id") or ""))
    body = read(entry / "entry.json")
    clashes = [item["path"] for item in body["files"] if (root / item["path"]).exists()]
    if clashes:
        raise WorkflowError(
            "restore_conflict", "a file with the same identity exists again", {"paths": clashes}
        )
    for item in body["files"]:
        target = root / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(entry / "files" / item["path"]), str(target))
    shutil.rmtree(entry)
    return {"restored": body["subject_id"], "kind": body["kind"]}


def trash_purge(request: dict, root: Path) -> dict[str, Any]:
    """Permanently remove trash entries. Requires ``confirm == "delete permanently"``."""
    if request.get("confirm") != "delete permanently":
        raise WorkflowError(
            "confirmation_required", 'set confirm to "delete permanently"', {"field": "confirm"}
        )
    wanted = request.get("entry_ids")
    older = request.get("older_than_days")
    if wanted:
        entries = [_entry_dir(root, str(item)) for item in wanted]
    else:
        entries = [path.parent for path in (root / "trash").glob("*/entry.json")]
        if older is not None:
            limit = time.time() - float(older) * 86400
            entries = [
                entry
                for entry in entries
                if datetime.fromisoformat(str(read(entry / "entry.json")["deleted_at"])).timestamp()
                < limit
            ]
    for entry in entries:
        shutil.rmtree(entry)
    return {"purged": len(entries)}


def set_status(saved: dict) -> str:
    """Whether a saved set matches the current replay rules."""
    if saved.get("recipe") == "missing_inputs":
        return "not_replayable"
    if saved.get("research_policy") in CURRENT_POLICIES:
        return "current"
    if saved.get("research_policy") in PREVIOUS_POLICIES:
        return "legacy_policy"
    return "not_replayable"


def run_status(experiment: dict, selection_exists: bool) -> str:
    """Whether a run was produced under the current scenarios and still has its set."""
    if not selection_exists:
        return "set_deleted"
    if experiment.get("scenario_name") in CURRENT_SCENARIOS:
        return "current"
    return "legacy_scenario"
