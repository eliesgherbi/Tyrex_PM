"""Job directory, content fingerprint, and atomic workflow records."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tyrex_pm.data_warehouse.capture.files import atomic_json, read_json
from tyrex_pm.data_warehouse.contracts import canonical, digest
from tyrex_pm.execution.process_lock import StateDirectoryBusy, StateDirectoryLock

REPO = Path(__file__).resolve().parents[4]
LOCK_NAME = ".workflow_job.lock"
DIGEST_SCHEME = "workflow-source-v1"
CODE_TREES = (
    "src/tyrex_pm/research/workflows",
    "src/tyrex_pm/research/cli.py",
    "src/tyrex_pm/research/strategy_replay",
    "src/tyrex_pm/research/replay",
    "src/tyrex_pm/research/strategy_lab/qualification.py",
    "src/tyrex_pm/research/strategy_lab/staged.py",
    "src/tyrex_pm/research/strategy_lab/timelines.py",
    "src/tyrex_pm/research/strategy_lab/frames.py",
    "src/tyrex_pm/data_warehouse/query",
    "src/tyrex_pm/strategies",
    "src/tyrex_pm/runtime",
    "src/tyrex_pm/execution",
    "src/tyrex_pm/protection",
    "config/research/scenarios",
)


def background_flags() -> int:
    """Windows creation flags for a background worker: a hidden console of its own.

    DETACHED_PROCESS (no console at all) made every console child of the worker, such as
    replay pool processes, open a visible window; closing one killed that replay.
    """
    if os.name != "nt":
        return 0
    return subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_root() -> Path:
    return REPO / "var" / "research" / "workflows"


def content_fingerprint(paths: list[Path]) -> str:
    hasher = hashlib.sha256()
    count = 0
    for path in paths:
        if path.is_dir():
            files = sorted(
                p for p in path.rglob("*") if p.is_file() and p.suffix in {".py", ".yaml", ".yml"}
            )
        elif path.is_file():
            files = [path]
        else:
            continue
        for file in files:
            try:
                relative = file.resolve().relative_to(REPO)
            except ValueError:
                relative = file
            hasher.update(str(relative).replace("\\", "/").encode())
            hasher.update(file.read_bytes())
            count += 1
    hasher.update(str(count).encode())
    return hasher.hexdigest()


def source_fingerprint() -> str:
    hook = os.environ.get("TYREX_WORKFLOW_FINGERPRINT_FILE")
    if hook:
        return Path(hook).read_text(encoding="utf-8").strip()
    paths = [REPO / relative for relative in CODE_TREES]
    return f"{DIGEST_SCHEME}:{content_fingerprint(paths)}"


def plan_digest(payload: Any) -> str:
    return digest(canonical(payload))


def slice_page(items: list, request: dict) -> tuple[list, int | None]:
    """Return one page and the next cursor. Omitted limit returns the first 100 rows."""
    from .contracts import WorkflowError

    try:
        limit = int(request.get("limit") or 100)
        cursor = int(request.get("cursor") or 0)
    except (TypeError, ValueError) as exc:
        raise WorkflowError("invalid_page", "limit and cursor are integers", {}) from exc
    if limit < 1 or cursor < 0:
        raise WorkflowError("invalid_page", "limit and cursor are out of range", {})
    page = list(items)[cursor : cursor + limit]
    next_cursor = cursor + limit if cursor + limit < len(items) else None
    return page, next_cursor


def write_json(path: Path, value: Any) -> None:
    """Publish JSON, retrying Windows sharing violations after atomic_json gives up.

    Status readers and a worker exiting in the same directory can keep a destination
    unreplaceable for longer than one rename retry window.
    """
    for attempt in range(6):
        try:
            atomic_json(path, value)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 5:
                raise
            time.sleep(0.2)


def read(path: Path) -> Any:
    # On Windows a reader can briefly get PermissionError while a writer atomically replaces
    # the same file (status polling during a running job). The replace finishes within
    # milliseconds; retry a few times before giving up.
    import time

    for attempt in range(5):
        try:
            return read_json(path)
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.02 * (attempt + 1))


def lock_held(directory: Path) -> bool:
    lock = StateDirectoryLock(directory, filename=LOCK_NAME)
    try:
        lock.acquire()
    except StateDirectoryBusy:
        return True
    lock.release()
    return False


def acquire(directory: Path) -> StateDirectoryLock:
    lock = StateDirectoryLock(directory, filename=LOCK_NAME)
    lock.acquire()
    return lock


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode()
