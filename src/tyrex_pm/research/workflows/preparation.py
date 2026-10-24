"""Managed preparation jobs. They do not rewrite saved window sets."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from .artifacts import background_flags, now_iso, plan_digest, read, write_json
from .contracts import WorkflowError

OWNERS = {
    "normalize": "normalize_once",
    "quality": "publish_quality",
    "verify": "verify",
}
LOCK_ROLES = ("normalizer", "quality-publisher", "quality-index", "collector")


def role_held(root: Path, role: str) -> bool:
    path = root / f".{role}.lock"
    if not path.is_file():
        return False
    handle = path.open("a+b")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return False
        import fcntl

        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def _busy_roles(warehouse: Path) -> list[str]:
    return [role for role in LOCK_ROLES if role_held(warehouse, role)]


def _dir(root: Path, preparation_id: str) -> Path:
    return root / "preparations" / preparation_id


def preparation_plan(request: dict, root: Path) -> dict[str, Any]:
    kind = request.get("kind")
    warehouse = request.get("warehouse")
    collection = request.get("collection")
    if kind not in OWNERS:
        raise WorkflowError(
            "preparation_kind",
            "preparation runs normalize, quality, or verify on data already recorded",
            {"kind": kind},
        )
    if not warehouse or not collection:
        raise WorkflowError("missing_field", "warehouse and collection are required", {})
    preparation_id = request.get("preparation_id") or uuid.uuid4().hex
    body = {
        "schema": "workflow_preparation_plan_v1",
        "preparation_id": preparation_id,
        "kind": kind,
        "owner": OWNERS[kind],
        "warehouse": str(Path(warehouse)),
        "collection": collection,
        "scope": "collection",
        "narrows_to_recipe_dates": False,
        "lock_roles": list(LOCK_ROLES),
        "safe_stop": "unsupported",
        "created_at": now_iso(),
        "reason": "normalize and quality run for the whole collection, not one window",
    }
    body["plan_id"] = plan_digest(
        {
            key: body[key]
            for key in ("kind", "owner", "warehouse", "collection", "scope", "safe_stop")
        }
    )
    path = _dir(root, preparation_id)
    path.mkdir(parents=True, exist_ok=True)
    write_json(path / "plan.json", body)
    return body


def preparation_start(request: dict, root: Path) -> dict[str, Any]:
    preparation_id = request.get("preparation_id")
    if not preparation_id:
        raise WorkflowError("missing_field", "preparation_id is required", {})
    folder = _dir(root, preparation_id)
    plan_path = folder / "plan.json"
    if not plan_path.exists():
        raise WorkflowError(
            "not_found", "preparation plan is not saved", {"preparation_id": preparation_id}
        )
    plan = read(plan_path)
    if request.get("plan_id") and request["plan_id"] != plan["plan_id"]:
        raise WorkflowError(
            "preview_stale", "the preparation plan changed", {"plan_id": plan["plan_id"]}
        )
    warehouse = Path(plan["warehouse"])
    busy = _busy_roles(warehouse)
    if busy:
        raise WorkflowError(
            "owner_busy",
            "a collector or preparation owner already holds this warehouse",
            {"roles": busy},
        )
    status_path = folder / "status.json"
    if status_path.exists():
        current = read(status_path)
        if current.get("state") == "running":
            raise WorkflowError(
                "owner_busy",
                "this preparation is already running",
                {"preparation_id": preparation_id},
            )
    status = {
        "state": "running",
        "generation": int((read(status_path).get("generation") if status_path.exists() else 0) or 0)
        + 1,
        "kind": plan["kind"],
        "safe_stop": "unsupported",
        "stop_requested": False,
        "owner_interrupted": False,
        "started_at": now_iso(),
        "error": None,
        "owner_result": None,
    }
    write_json(status_path, status)
    if request.get("inline"):
        from .preparation_worker import run_preparation

        code = run_preparation(folder)
        return {
            "preparation_id": preparation_id,
            "state": read(status_path)["state"],
            "inline": True,
            "code": code,
        }
    _spawn(folder)
    return {
        "preparation_id": preparation_id,
        "state": "running",
        "generation": status["generation"],
        "inline": False,
    }


def preparation_status(request: dict, root: Path) -> dict[str, Any]:
    preparation_id = request.get("preparation_id")
    folder = _dir(root, str(preparation_id))
    status_path = folder / "status.json"
    if not status_path.exists():
        raise WorkflowError(
            "not_found", "preparation has not started", {"preparation_id": preparation_id}
        )
    status = read(status_path)
    plan = read(folder / "plan.json") if (folder / "plan.json").exists() else {}
    status.update(
        {
            "preparation_id": preparation_id,
            "scope": plan.get("scope"),
            "safe_stop": "unsupported",
            "owner": plan.get("owner"),
        }
    )
    return status


def preparation_stop(request: dict, root: Path) -> dict[str, Any]:
    preparation_id = request.get("preparation_id")
    folder = _dir(root, str(preparation_id))
    status_path = folder / "status.json"
    if not status_path.exists():
        raise WorkflowError(
            "not_found", "preparation has not started", {"preparation_id": preparation_id}
        )
    status = read(status_path)
    payload = {
        "generation": status.get("generation"),
        "requested_at": now_iso(),
        "safe_stop": "unsupported",
    }
    write_json(folder / "stop.json", payload)
    status["stop_requested"] = True
    status["safe_stop"] = "unsupported"
    status["owner_interrupted"] = False
    write_json(status_path, status)
    return {
        "preparation_id": preparation_id,
        "stop_generation": payload["generation"],
        "safe_stop": "unsupported",
        "owner_interrupted": False,
    }


def _spawn(folder: Path) -> None:
    log = (folder / "worker.log").open("ab")
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tyrex_pm.research.workflows.preparation_worker",
                "--job",
                str(folder),
            ],
            cwd=str(Path(__file__).resolve().parents[4]),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            creationflags=background_flags(),
        )
    finally:
        log.close()
