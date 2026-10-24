"""Start, watch and stop a market-data recording from the workspace.

A recording runs the existing collector (``data_warehouse.cli.live_main``) as its own
process. It reads public market data and the reference feed only; it places no order and
starts no paper session. One recording at a time per warehouse.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tyrex_pm.data_warehouse.capture.files import read_json
from tyrex_pm.data_warehouse.supervisor import collector_stop_matches

from .artifacts import REPO, background_flags, now_iso, read, write_json
from .contracts import WorkflowError

PROFILE_PATH = REPO / "config" / "data" / "btc_5m.yaml"
FALLBACK_PROFILE = REPO / "src" / "tyrex_pm" / "data_warehouse" / "profiles" / "btc_5m.yaml"
DOTENV = REPO / ".env"
MIN_DURATION_S = 300
MAX_DURATION_S = 24 * 3600
HEARTBEAT_STALE_S = 45
WINDOW_S = 300
# Kept in step with supervisor.CLOSE_MARGIN_S (data recorded after the last close).
CLOSE_MARGIN_S = 30
# Winners of BTC 5-minute markets usually appear a few minutes after the close.
OUTCOME_WAIT_DEFAULT_S = 15 * 60
OUTCOME_WAIT_MAX_S = 60 * 60


def warehouse_roots() -> list[Path]:
    """Warehouses the workspace reads and may record into."""
    raw = os.environ.get("TYREX_WAREHOUSE_ROOTS")
    if raw:
        return [Path(item).resolve() for item in raw.split(os.pathsep) if item]
    known = ("warehouse_btc5m", "warehouse_btc5m_s3", "warehouse", "warehouse_phase_b_validation")
    candidates = [REPO / "var" / name for name in known]
    # Folders created by recordings started from the workspace.
    candidates += sorted((REPO / "var").glob("warehouse_rec_*"))
    return [path.resolve() for path in candidates if (path / "collections").is_dir()]


def _profile_path() -> Path:
    return PROFILE_PATH if PROFILE_PATH.is_file() else FALLBACK_PROFILE


def _record_dir(root: Path) -> Path:
    return root / "recordings"


def _credentials_present() -> bool:
    from tyrex_pm.data_warehouse.config import load_credentials

    try:
        return load_credentials(DOTENV, required=True) is not None
    except Exception:  # noqa: BLE001 - only presence is reported, never values
        return False


PROBES = {
    "Polymarket markets (Gamma)": "https://gamma-api.polymarket.com/markets?limit=1",
    "Polymarket order books (CLOB)": "https://clob.polymarket.com/time",
    "Polymarket reference feed": "https://ws-live-v2.polymarket.com/",
    "Polymarket winners (Data API)": "https://data-api.polymarket.com/",
    "Binance": "https://api.binance.com/api/v3/time",
}
REQUIRED_PROBES = (
    "Polymarket markets (Gamma)",
    "Polymarket order books (CLOB)",
    "Polymarket reference feed",
)


def reachability(timeout_s: float = 6.0) -> list[dict[str, Any]]:
    """Can this machine reach the venues? A TLS failure usually means a network-level block."""
    import ssl
    import urllib.error
    import urllib.request

    rows = []
    for name, url in PROBES.items():
        try:
            with urllib.request.urlopen(
                url, timeout=timeout_s, context=ssl.create_default_context()
            ):
                ok, reason = True, ""
        except urllib.error.HTTPError as exc:
            # Any HTTP answer proves the host is reachable over verified TLS.
            ok, reason = True, f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 - only the failure class is reported
            text = str(getattr(exc, "reason", exc))
            ok = False
            reason = (
                "TLS certificate rejected"
                if "CERTIFICATE" in text.upper()
                else type(getattr(exc, "reason", exc)).__name__
            )
        rows.append({"name": name, "url": url, "ok": ok, "reason": reason})
    return rows


def _new_warehouse_name() -> str:
    return "warehouse_rec_" + datetime.now(UTC).strftime("%Y%m%d")


def _live(record: dict) -> dict:
    """Join the control record with the collector's own log line and heartbeat."""
    view = dict(record)
    log = Path(record["log_path"])
    if not view.get("session_id") and log.is_file():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("{") and '"collection_id"' in line and '"session_id"' in line:
                import json

                first = json.loads(line)
                view["collection"] = first["collection_id"]
                view["session_id"] = first["session_id"]
                break
    status = None
    if view.get("collection"):
        status_path = Path(record["warehouse"]) / "collections" / view["collection"] / "status.json"
        if status_path.is_file():
            try:
                status = read_json(status_path)
            except (OSError, ValueError):
                status = None
    view["host_status"] = _summary(status)
    started = datetime.fromisoformat(record["started_at"]).timestamp()
    view["elapsed_s"] = max(0.0, time.time() - started)
    view["remaining_s"] = max(0.0, float(record["duration_s"]) - view["elapsed_s"])
    heartbeat = (status or {}).get("heartbeat_at")
    age = (time.time_ns() - int(heartbeat)) / 10**9 if heartbeat else None
    # A session's own heartbeat is the liveness signal; a heartbeat from before this
    # recording started belongs to an earlier session of the same collection.
    fresh = age is not None and heartbeat and int(heartbeat) >= started * 10**9
    if fresh and age <= HEARTBEAT_STALE_S:
        state = "recording"
    elif view["remaining_s"] <= 0 or record.get("stop_requested_at"):
        state = "finished"
    elif not fresh and view["elapsed_s"] < 120:
        state = "starting"
    else:
        state = "not_responding"
    view["state"] = state
    view["phase"] = (status or {}).get("phase") if state == "recording" else None
    view["heartbeat_age_s"] = age
    view["log_tail"] = _tail(log) if state in ("not_responding", "starting") else []
    return view


def _summary(status: dict | None) -> dict | None:
    if not status:
        return None
    feeds = status.get("feeds") or {}
    return {
        "uptime_s": status.get("uptime_s"),
        "markets": status.get("markets"),
        "book_markets": status.get("book_markets"),
        "subscribed_tokens": status.get("subscribed_tokens"),
        "feeds": {
            name: {"connected": bool(feed.get("connected")), "source": feed.get("source")}
            for name, feed in feeds.items()
        },
        "recent_gaps": status.get("recent_gaps"),
        "raw_bytes": status.get("raw_bytes"),
        "free_disk_bytes": status.get("free_disk_bytes"),
        "writer_healthy": status.get("writer_healthy"),
        "normalization_lag_s": status.get("normalization_lag_s"),
        "expected_window_statuses": status.get("expected_window_statuses"),
        "stopped": status.get("stopped"),
        "phase": status.get("phase"),
        "capture_until": status.get("capture_until"),
        "outcomes": status.get("outcomes"),
    }


def _tail(log: Path, lines: int = 8) -> list[str]:
    if not log.is_file():
        return []
    text = log.read_text(encoding="utf-8", errors="replace").splitlines()
    return [line[:300] for line in text[-lines:]]


def _records(root: Path) -> list[dict]:
    folder = _record_dir(root)
    return [read(path) for path in sorted(folder.glob("*.json"))] if folder.is_dir() else []


def recording_plan(request: dict, root: Path) -> dict[str, Any]:
    del request
    roots = warehouse_roots()
    # Whole-warehouse footprint (raw + normalized + catalog) measured on the 6, 15 and 19 Oct
    # recordings: 9.2 GB / 5 h, 12 GB / 6 h, 9.5 GB / 6 h. Those used the old ~700-token
    # subscription; recordings after the fix should need less.
    rate = 1.8e9
    target = REPO / "var"
    usage = shutil.disk_usage(target)
    running = [
        view for view in map(_live, _records(root)) if view["state"] in ("starting", "recording")
    ]
    return {
        "profile": {
            "id": "btc-5m",
            "path": str(_profile_path().relative_to(REPO)).replace("\\", "/"),
        },
        "warehouses": [
            {
                "path": str(path),
                "name": path.name,
                "collections": len(list((path / "collections").glob("*/collection.json"))),
                "busy": any(Path(view["warehouse"]) == path for view in running),
            }
            for path in roots
        ],
        "new_warehouse_name": _new_warehouse_name(),
        "duration": {"min_s": MIN_DURATION_S, "max_s": MAX_DURATION_S, "default_s": 6 * 3600},
        "bytes_per_hour_estimate": rate,
        "bytes_per_hour_basis": "measured on earlier recordings before the subscription fix",
        "free_disk_bytes": usage.free,
        "credentials_present": _credentials_present(),
        "network": reachability(),
        "normalize_live_default": True,
        "closing": {
            "window_s": WINDOW_S,
            "close_margin_s": CLOSE_MARGIN_S,
            "finish_windows_default": True,
            "outcome_wait_default_s": OUTCOME_WAIT_DEFAULT_S,
            "outcome_wait_max_s": OUTCOME_WAIT_MAX_S,
            "basis": (
                "The last window is recorded to its close plus 30 s. Afterwards only the "
                "provider's resolution and market metadata are polled, until every recorded "
                "window has a winner or the wait ends; no books are recorded in that phase."
            ),
        },
        "subscription": (
            "Books from 15 minutes before each market opens until 2 minutes after it closes."
        ),
        "running": running,
        "places_orders": False,
    }


def recording_start(request: dict, root: Path) -> dict[str, Any]:
    try:
        duration_s = int(request.get("duration_s") or 0)
    except (TypeError, ValueError) as exc:
        raise WorkflowError(
            "invalid_duration", "duration_s is a whole number of seconds", {}
        ) from exc
    if not MIN_DURATION_S <= duration_s <= MAX_DURATION_S:
        raise WorkflowError(
            "invalid_duration",
            f"a recording lasts between {MIN_DURATION_S // 60} minutes and 24 hours",
            {"duration_s": duration_s},
        )
    finish_windows = request.get("finish_windows") is not False
    outcome_wait_s = _outcome_wait(request)
    roots = warehouse_roots()
    if request.get("new_warehouse"):
        warehouse = (REPO / "var" / _new_warehouse_name()).resolve()
        (warehouse / "collections").mkdir(parents=True, exist_ok=True)
    else:
        warehouse = Path(str(request.get("warehouse") or "")).resolve()
        if warehouse not in roots:
            raise WorkflowError(
                "root_not_allowed",
                "record into a configured warehouse",
                {"warehouse": str(warehouse)},
            )
    for view in map(_live, _records(root)):
        if Path(view["warehouse"]) == warehouse and view["state"] in ("starting", "recording"):
            raise WorkflowError(
                "recording_running",
                "a recording is already running in this warehouse",
                {"recording_id": view["recording_id"]},
            )
    blocked = [row for row in reachability() if row["name"] in REQUIRED_PROBES and not row["ok"]]
    if blocked:
        raise WorkflowError(
            "venue_unreachable",
            "Polymarket cannot be reached from this network; nothing useful would be recorded",
            {"unreachable": [f"{row['name']}: {row['reason']}" for row in blocked]},
        )
    if not _credentials_present():
        raise WorkflowError(
            "credentials_missing",
            "the reference feed needs TYREX_DATA_PM (or POLYMARKET) API credentials in .env",
            {},
        )
    recording_id = uuid.uuid4().hex
    folder = _record_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    log_path = folder / f"{recording_id}.log"
    argv = [
        sys.executable,
        "-c",
        "from tyrex_pm.data_warehouse.cli import live_main; raise SystemExit(live_main())",
        "--root",
        str(warehouse),
        "--config",
        str(_profile_path()),
        "--dotenv",
        str(DOTENV),
        "--duration",
        f"{duration_s}s",
    ]
    if request.get("normalize_live") is False:
        argv.append("--no-normalize")
    if finish_windows:
        argv.append("--finish-windows")
    if outcome_wait_s:
        argv.extend(["--outcome-wait", f"{outcome_wait_s}s"])
    env = os.environ.copy()
    env["TYREX_COLLECTION_GENERATION"] = "1"
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            argv,
            cwd=str(REPO),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=background_flags(),
        )
    record = {
        "schema": "workflow_recording_v1",
        "recording_id": recording_id,
        "name": str(request.get("name") or "").strip() or f"Recording {now_iso()[:16]}",
        "notes": str(request.get("notes") or ""),
        "warehouse": str(warehouse),
        "profile": str(_profile_path()),
        "duration_s": duration_s,
        "finish_windows": finish_windows,
        "outcome_wait_s": outcome_wait_s,
        "normalize_live": request.get("normalize_live") is not False,
        "started_at": now_iso(),
        "pid": process.pid,
        "generation": "1",
        "log_path": str(log_path),
        "collection": None,
        "session_id": None,
    }
    write_json(folder / f"{recording_id}.json", record)
    return {"recording_id": recording_id, "warehouse": str(warehouse), "pid": process.pid}


def _outcome_wait(request: dict) -> int:
    raw = request.get("outcome_wait_s", OUTCOME_WAIT_DEFAULT_S)
    try:
        value = int(raw or 0)
    except (TypeError, ValueError) as exc:
        raise WorkflowError("invalid_outcome_wait", "outcome_wait_s is whole seconds", {}) from exc
    if not 0 <= value <= OUTCOME_WAIT_MAX_S:
        raise WorkflowError(
            "invalid_outcome_wait",
            "the wait for winners lasts between 0 and 60 minutes",
            {"outcome_wait_s": value},
        )
    return value


def recording_fetch_outcomes(request: dict, root: Path) -> dict[str, Any]:
    """Poll the provider for the winners a recording did not observe. Records nothing else.

    Runs the collector on the recording's collection in outcomes-only mode: a short
    session that asks the resolution endpoint about the collection's recorded windows
    without a winner and stores the answers, received now, like any other observation.
    """
    from .inventory import capture_blocks

    block_id = str(request.get("block_id") or "")
    block = next(
        (item for item in capture_blocks(warehouse_roots()) if item["block_id"] == block_id),
        None,
    )
    if block is None:
        raise WorkflowError("not_found", "recording is not known", {"block_id": block_id})
    warehouse = Path(block["warehouse"])
    for view in map(_live, _records(root)):
        if Path(view["warehouse"]) == warehouse and view["state"] in ("starting", "recording"):
            raise WorkflowError(
                "recording_running",
                "this warehouse is in use by a recording or a winner fetch; wait until it ends",
                {"recording_id": view["recording_id"]},
            )
    needed = ("Polymarket winners (Data API)",)
    blocked = [row for row in reachability() if row["name"] in needed and not row["ok"]]
    if blocked:
        raise WorkflowError(
            "venue_unreachable",
            "Polymarket's resolution service cannot be reached from this network",
            {"unreachable": [f"{row['name']}: {row['reason']}" for row in blocked]},
        )
    wait_s = _outcome_wait({"outcome_wait_s": request.get("outcome_wait_s", 10 * 60)}) or 60
    recording_id = uuid.uuid4().hex
    folder = _record_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    log_path = folder / f"{recording_id}.log"
    argv = [
        sys.executable,
        "-c",
        "from tyrex_pm.data_warehouse.cli import live_main; raise SystemExit(live_main())",
        "--root",
        str(warehouse),
        "--resume",
        block["collection"],
        "--dotenv",
        str(DOTENV),
        "--outcomes-only",
        "--outcome-wait",
        f"{wait_s}s",
    ]
    env = os.environ.copy()
    env["TYREX_COLLECTION_GENERATION"] = "1"
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            argv,
            cwd=str(REPO),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=background_flags(),
        )
    record = {
        "schema": "workflow_recording_v1",
        "kind": "outcome_fetch",
        "recording_id": recording_id,
        "name": f"Fetch winners · {block_id.split(':')[0][:8]}",
        "notes": f"winners for recording {block_id}",
        "warehouse": str(warehouse),
        "profile": None,
        "duration_s": wait_s,
        "normalize_live": True,
        "started_at": now_iso(),
        "pid": process.pid,
        "generation": "1",
        "log_path": str(log_path),
        "collection": block["collection"],
        "session_id": None,
        "block_id": block_id,
    }
    write_json(folder / f"{recording_id}.json", record)
    return {"recording_id": recording_id, "warehouse": str(warehouse), "wait_s": wait_s}


def recording_status(request: dict, root: Path) -> dict[str, Any]:
    path = _record_dir(root) / f"{request.get('recording_id')}.json"
    if not path.is_file():
        raise WorkflowError(
            "not_found", "recording is not known", {"recording_id": request.get("recording_id")}
        )
    record = read(path)
    view = _live(record)
    if view.get("collection") and not record.get("collection"):
        record.update(collection=view["collection"], session_id=view["session_id"])
        write_json(path, record)
    return view


def recording_list(request: dict, root: Path) -> dict[str, Any]:
    del request
    rows = [_live(record) for record in _records(root)]
    rows.sort(key=lambda item: item["started_at"], reverse=True)
    return {"recordings": rows}


def recording_stop(request: dict, root: Path) -> dict[str, Any]:
    view = recording_status(request, root)
    if not view.get("collection") or not view.get("session_id"):
        raise WorkflowError(
            "not_started", "the collector has not reported its session yet; try again shortly", {}
        )
    payload = {
        "session_id": view["session_id"],
        "generation": view["generation"],
        "requested_at": now_iso(),
    }
    if not collector_stop_matches(
        payload, session_id=view["session_id"], generation=view["generation"]
    ):
        raise WorkflowError("stop_target", "stop payload does not match the collector rule", {})
    target = (
        Path(view["warehouse"])
        / "collections"
        / view["collection"]
        / "sessions"
        / f"{view['session_id']}.collector-stop"
    )
    write_json(target, payload)
    path = _record_dir(root) / f"{view['recording_id']}.json"
    record = read(path)
    record["stop_requested_at"] = now_iso()
    write_json(path, record)
    return {"recording_id": view["recording_id"], "stop_requested": True}
