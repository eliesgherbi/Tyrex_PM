"""Collection control around the existing collector. Preparation is a different operation."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tyrex_pm.data_warehouse.capture.files import read_json
from tyrex_pm.data_warehouse.supervisor import collector_stop_matches, resume_profile

from .artifacts import now_iso, read, write_json
from .contracts import WorkflowError


def _record_path(root: Path, collection: str) -> Path:
    return root / "collections" / f"{collection}.json"


def collection_start(request: dict, root: Path) -> dict[str, Any]:
    warehouse = Path(request["warehouse"])
    collection = request.get("collection")
    resume = request.get("resume")
    if resume:
        try:
            profile = resume_profile(warehouse, resume)
        except Exception as exc:  # noqa: BLE001
            raise WorkflowError(
                "collection_profile",
                str(exc),
                {"error_type": type(exc).__name__, "collection": resume},
            ) from exc
        collection = resume
    else:
        config = request.get("config")
        if not config:
            raise WorkflowError(
                "missing_field", "config or resume is required", {"field": "config"}
            )
        from tyrex_pm.data_warehouse.config import load_profile

        profile = load_profile(Path(config))
        collection = collection or "pending"
    generation = str(request.get("generation") or "1")
    record = {
        "collection": collection,
        "warehouse": str(warehouse),
        "profile_hash": profile.hash,
        "generation": generation,
        "session_id": request.get("session_id"),
        "started_at": now_iso(),
        "launched": bool(request.get("launch")),
    }
    write_json(_record_path(root, collection), record)
    launched = False
    if request.get("launch"):
        env = os.environ.copy()
        env["TYREX_COLLECTION_GENERATION"] = generation
        command = [sys.executable, "-m", "tyrex_pm.data_warehouse.cli"]
        # The live entry point is run_data_collection_live, not a tyrex-data subcommand.
        command = [
            sys.executable,
            "-c",
            "from tyrex_pm.data_warehouse.cli import live_main; raise SystemExit(live_main())",
        ]
        argv = ["--root", str(warehouse)]
        if resume:
            argv.extend(["--resume", resume])
        else:
            argv.extend(["--config", str(request["config"])])
        if request.get("duration"):
            argv.extend(["--duration", str(request["duration"])])
        subprocess.Popen(
            [command[0], command[1], command[2], *argv],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        launched = True
    return {
        "collection": collection,
        "profile_hash": profile.hash,
        "generation": generation,
        "launched": launched,
        "prepares_only": False,
    }


def collection_status(request: dict, root: Path) -> dict[str, Any]:
    warehouse = Path(request["warehouse"])
    collection = request["collection"]
    status_path = warehouse / "collections" / collection / "status.json"
    status = read_json(status_path) if status_path.exists() else None
    record_path = _record_path(root, collection)
    record = read(record_path) if record_path.exists() else None
    return {"collection": collection, "host_status": status, "workflow": record, "mutates": False}


def collection_stop(request: dict, root: Path) -> dict[str, Any]:
    collection = request["collection"]
    record_path = _record_path(root, collection)
    if not record_path.exists():
        raise WorkflowError(
            "not_found", "collection control record is missing", {"collection": collection}
        )
    record = read(record_path)
    session_id = str(request.get("session_id") or "")
    generation = str(request.get("generation") or "")
    if session_id != str(record.get("session_id") or "") or generation != str(
        record.get("generation")
    ):
        raise WorkflowError(
            "stop_target",
            "stop requests name the recorded session and generation",
            {
                "session_id": session_id,
                "generation": generation,
                "recorded_session_id": record.get("session_id"),
                "recorded_generation": record.get("generation"),
            },
        )
    payload = {"session_id": session_id, "generation": generation, "requested_at": now_iso()}
    if not collector_stop_matches(payload, session_id=session_id, generation=generation):
        raise WorkflowError("stop_target", "stop payload does not match the collector rule", {})
    warehouse = Path(record["warehouse"])
    target = warehouse / "collections" / collection / "sessions" / f"{session_id}.collector-stop"
    write_json(target, payload)
    return {
        "collection": collection,
        "session_id": session_id,
        "generation": generation,
        "path": str(target),
    }


def collection_resume(request: dict, root: Path) -> dict[str, Any]:
    request = dict(request)
    request["resume"] = request["collection"]
    request["generation"] = str(int(request.get("generation") or "1") + 1)
    return collection_start(request, root)
