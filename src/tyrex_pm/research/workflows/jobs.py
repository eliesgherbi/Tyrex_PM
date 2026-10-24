"""Durable local jobs. The worker process owns the lock; viewers only read."""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from tyrex_pm.execution.process_lock import StateDirectoryBusy

from .artifacts import (
    acquire,
    background_flags,
    file_sha256,
    lock_held,
    now_iso,
    plan_digest,
    read,
    source_fingerprint,
    write_json,
)
from .contracts import WorkflowError

PARTS = "parts"
_UNSAFE_PART_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def part_path(directory: Path, unit_id: str, suffix: str = ".json") -> Path:
    """File name for a unit. The unit id stays in the part record; Windows rejects ':'."""
    text = str(unit_id)
    cleaned = _UNSAFE_PART_NAME.sub("_", text).rstrip(" .")
    if cleaned == text and cleaned not in {"", ".", ".."}:
        name = cleaned
    else:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        stem = cleaned[:80] if cleaned not in {"", ".", ".."} else "unit"
        name = f"{stem}_{digest}"
    return directory / f"{name}{suffix}"


def part_sha256(path: Path) -> str:
    return file_sha256(path)


def _job_dir(root: Path, job_id: str) -> Path:
    return root / "jobs" / job_id


def _status_path(job_dir: Path) -> Path:
    return job_dir / "status.json"


def _stop_applies(payload: dict, generation: int) -> bool:
    return int(payload.get("generation", -1)) == int(generation)


def stop_applies(payload: dict, generation: int) -> bool:
    return _stop_applies(payload, generation)


def _read_status(job_dir: Path) -> dict:
    path = _status_path(job_dir)
    if not path.exists():
        raise WorkflowError("not_found", "job status is missing", {"path": str(path)})
    return read(path)


def _job(root: Path, job_id: str) -> tuple[Path, dict]:
    job_dir = _job_dir(root, job_id)
    path = job_dir / "job.json"
    if not path.exists():
        raise WorkflowError("not_found", "job is not saved", {"job_id": job_id})
    return job_dir, read(path)


def verified_unit_ids(job_dir: Path) -> set[str]:
    manifest_path = job_dir / PARTS / "manifest.json"
    if not manifest_path.exists():
        return set()
    manifest = read(manifest_path)
    done = set()
    for unit_id, entry in (manifest.get("parts") or {}).items():
        target = part_path(job_dir / PARTS, unit_id)
        if not target.is_file() or file_sha256(target) != entry.get("sha256"):
            continue
        trace_sha = entry.get("trace_sha256")
        if trace_sha:
            trace = part_path(job_dir / PARTS, unit_id, ".decisions.json")
            if not trace.is_file() or file_sha256(trace) != trace_sha:
                continue
        done.add(unit_id)
    return done


def quarantine_partials(job_dir: Path) -> list[str]:
    parts = job_dir / PARTS
    if not parts.is_dir():
        return []
    quarantine = parts / "quarantine"
    moved = []
    for tmp in sorted(parts.glob("*.json.tmp")):
        quarantine.mkdir(parents=True, exist_ok=True)
        dest = quarantine / f"{tmp.name}.{uuid.uuid4().hex}"
        tmp.replace(dest)
        moved.append(str(dest))
    return moved


def _spawn(job_dir: Path) -> None:
    log_path = job_dir / "worker.log"
    log = log_path.open("ab")
    try:
        subprocess.Popen(
            [sys.executable, "-m", "tyrex_pm.research.workflows.worker", "--job", str(job_dir)],
            cwd=str(Path(__file__).resolve().parents[4]),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            creationflags=background_flags(),
        )
    finally:
        log.close()


def _wait_until_owned(job_dir: Path, generation: int, timeout_s: float) -> dict:
    deadline = time.time() + timeout_s
    terminal = {"failed", "completed", "interrupted", "cancelled"}
    while time.time() < deadline:
        path = _status_path(job_dir)
        if path.exists():
            status = read(path)
            same = (
                status.get("owner_acknowledged") and int(status.get("generation", -1)) == generation
            )
            if same and lock_held(job_dir):
                return status
            if status.get("state") in terminal:
                return status
            # Acknowledged and then exited (crash or kill) before the parent observed the lock.
            if same and status.get("state") != "preparing" and not lock_held(job_dir):
                time.sleep(0.05)
                if not lock_held(job_dir):
                    return read(path)
        time.sleep(0.05)
    raise WorkflowError(
        "worker_start_timeout",
        "the worker did not acknowledge the lock",
        {"job_dir": str(job_dir)},
    )


def job_start(request: dict, root: Path) -> dict[str, Any]:
    experiment_id = request.get("experiment_id")
    experiment_path = root / "experiments" / f"{experiment_id}.json"
    if not experiment_path.exists():
        raise WorkflowError(
            "not_found", "experiment is not saved", {"experiment_id": experiment_id}
        )
    experiment = read(experiment_path)
    units = list(experiment.get("units") or [])
    block_id = request.get("block_id")
    if block_id:
        units = [unit for unit in units if unit.get("block_id") == block_id]
    if not units:
        raise WorkflowError(
            "empty_plan", "the experiment has no job units", {"experiment_id": experiment_id}
        )
    if experiment.get("mode") == "replay":
        jobs_dir = root / "jobs"
        if jobs_dir.is_dir():
            for path in jobs_dir.glob("*/job.json"):
                other = read(path)
                if other.get("experiment_id") == experiment_id:
                    raise WorkflowError(
                        "job_exists",
                        "this reviewed replay already has a job; resume it",
                        {"job_id": other.get("job_id")},
                    )
                # A second preview of the same inputs is a new experiment id with the
                # same digest. Refuse it while the first is still live or resumable.
                if other.get("experiment_digest") == experiment.get("digest"):
                    status_path = _status_path(path.parent)
                    state = read(status_path).get("state") if status_path.exists() else None
                    if state in ("preparing", "running", "interrupted"):
                        raise WorkflowError(
                            "identical_replay_active",
                            "an identical replay is already running or stopped; "
                            "open or resume it instead",
                            {"job_id": other.get("job_id"), "state": state},
                        )
    job_id = request.get("job_id") or uuid.uuid4().hex
    job_dir = _job_dir(root, job_id)
    if (job_dir / "job.json").exists():
        if lock_held(job_dir):
            status = _read_status(job_dir)
            raise WorkflowError(
                "job_busy",
                "the OS lock is held; heartbeat age does not authorize takeover",
                {
                    "heartbeat_at": status.get("heartbeat_at"),
                    "pid": status.get("pid"),
                    "generation": status.get("generation"),
                },
            )
        raise WorkflowError("job_exists", "job identity is immutable", {"job_id": job_id})
    fingerprint = source_fingerprint()
    body = {
        "schema": "workflow_job_v1",
        "job_id": job_id,
        "experiment_id": experiment_id,
        "experiment_digest": experiment["digest"],
        "created_at": now_iso(),
        "block_id": block_id,
        "units": units,
        "pins": experiment.get("pins") or {},
        "source_fingerprint": fingerprint,
    }
    body["plan_digest"] = plan_digest(
        {"units": units, "pins": body["pins"], "experiment_digest": experiment["digest"]}
    )
    job_dir.mkdir(parents=True, exist_ok=True)
    write_json(job_dir / "job.json", body)
    write_json(
        _status_path(job_dir),
        {
            "state": "preparing",
            "generation": 1,
            "pid": None,
            "owner_acknowledged": False,
            "heartbeat_at": None,
            "expected": len(units),
            "completed": 0,
            "failed": 0,
            "active_unit": None,
            "error": None,
            "source_fingerprint": fingerprint,
            "plan_digest": body["plan_digest"],
        },
    )
    _spawn(job_dir)
    status = _wait_until_owned(job_dir, 1, float(request.get("ack_timeout_s") or 15))
    return {
        "job_id": job_id,
        "state": status["state"],
        "generation": status["generation"],
        "pid": status.get("pid"),
    }


def job_status(request: dict, root: Path) -> dict[str, Any]:
    job_id = request["job_id"]
    job_dir, body = _job(root, job_id)
    status = _read_status(job_dir)
    heartbeat = status.get("heartbeat_at")
    age = None if heartbeat is None else (time.time_ns() - int(heartbeat)) / 1_000_000_000
    return {
        "job_id": job_id,
        "state": status["state"],
        "generation": status["generation"],
        "pid": status.get("pid"),
        "owner_acknowledged": status.get("owner_acknowledged"),
        "heartbeat_at": heartbeat,
        "heartbeat_age_s": age,
        "lock_held": lock_held(job_dir),
        "expected": status["expected"],
        "completed": status["completed"],
        "failed": status["failed"],
        "active_unit": status.get("active_unit"),
        "error": status.get("error"),
        "verified_units": sorted(verified_unit_ids(job_dir)),
        "plan_digest": body["plan_digest"],
    }


def job_stop(request: dict, root: Path) -> dict[str, Any]:
    job_id = request["job_id"]
    job_dir, _body = _job(root, job_id)
    status = _read_status(job_dir)
    generation = int(status["generation"])
    write_json(
        job_dir / "stop.json",
        {"generation": generation, "requested_at": now_iso()},
    )
    return {"job_id": job_id, "stop_generation": generation, "state": status["state"]}


def _identities_match(body: dict, status: dict) -> None:
    from .data import _verify_parts

    current = source_fingerprint()
    mismatches = {}
    if current != body["source_fingerprint"]:
        mismatches["source_fingerprint"] = {
            "expected": body["source_fingerprint"],
            "actual": current,
        }
    if status.get("plan_digest") != body["plan_digest"]:
        mismatches["plan_digest"] = {
            "expected": body["plan_digest"],
            "actual": status.get("plan_digest"),
        }
    if mismatches:
        raise WorkflowError("identity_mismatch", "resume inputs changed", mismatches)
    _verify_parts((body.get("pins") or {}).get("pin_parts") or [])


def request_pins(body: dict) -> dict:
    return dict(body.get("pins") or {})


def job_resume(request: dict, root: Path) -> dict[str, Any]:
    job_id = request["job_id"]
    job_dir, body = _job(root, job_id)
    if lock_held(job_dir):
        status = _read_status(job_dir)
        raise WorkflowError(
            "job_busy",
            "the OS lock is held; heartbeat age does not authorize takeover",
            {
                "heartbeat_at": status.get("heartbeat_at"),
                "pid": status.get("pid"),
                "generation": status.get("generation"),
            },
        )
    status = _read_status(job_dir)
    if status["state"] == "completed":
        return {
            "job_id": job_id,
            "state": "completed",
            "generation": status["generation"],
            "resumed": False,
            "verified_units": sorted(verified_unit_ids(job_dir)),
        }
    if status["state"] not in {
        "interrupted",
        "failed",
        "cancelled",
        "running",
        "preparing",
        "stopping",
    }:
        raise WorkflowError("resume_refused", "job state cannot resume", {"state": status["state"]})
    _identities_match(body, status)
    moved = quarantine_partials(job_dir)
    generation = int(status["generation"]) + 1
    try:
        lock = acquire(job_dir)
    except StateDirectoryBusy as exc:
        raise WorkflowError("job_busy", "the OS lock is held", {}) from exc
    try:
        write_json(
            _status_path(job_dir),
            {
                **status,
                "state": "preparing",
                "generation": generation,
                "owner_acknowledged": False,
                "pid": None,
                "error": None,
            },
        )
    finally:
        lock.release()
    _spawn(job_dir)
    acknowledged = _wait_until_owned(job_dir, generation, float(request.get("ack_timeout_s") or 15))
    return {
        "job_id": job_id,
        "state": acknowledged["state"],
        "generation": generation,
        "quarantined": moved,
        "resumed": True,
    }


def assert_compatible(body: dict, *, fingerprint: str, pins: dict) -> None:
    mismatches = {}
    if fingerprint != body["source_fingerprint"]:
        mismatches["source_fingerprint"] = {
            "expected": body["source_fingerprint"],
            "actual": fingerprint,
        }
    if pins != body.get("pins"):
        mismatches["pins"] = {"expected": body.get("pins"), "actual": pins}
    if mismatches:
        raise WorkflowError("identity_mismatch", "job inputs changed", mismatches)
