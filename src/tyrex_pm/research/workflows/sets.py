"""Window-set lineage and dependents, names and descriptions, membership export, and
windows reserved for a future Tune holdout.

A set's windows, pins and digest never change. Name and description are labels with an
edit history; versions are separate sets linked by ``parent_selection_id``.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from pathlib import Path
from typing import Any

from .artifacts import now_iso, read, write_json
from .contracts import WorkflowError


def _selection(root: Path, selection_id: str) -> dict:
    path = root / "selections" / f"{selection_id}.json"
    if not selection_id or not path.is_file():
        raise WorkflowError("not_found", "window set is not saved", {"selection_id": selection_id})
    return read(path)


def _jobs_by_experiment(root: Path) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for path in (root / "jobs").glob("*/job.json") if (root / "jobs").is_dir() else []:
        body = read(path)
        status_path = path.parent / "status.json"
        status = read(status_path) if status_path.is_file() else {}
        found.setdefault(str(body.get("experiment_id")), []).append(
            {
                "job_id": body.get("job_id"),
                "created_at": body.get("created_at"),
                "state": status.get("state"),
                "completed": status.get("completed"),
                "expected": status.get("expected"),
            }
        )
    return found


def selection_lineage(request: dict, root: Path) -> dict[str, Any]:
    """Versions before and after a set, the runs that used it, and candidates from them."""
    selection_id = str(request.get("selection_id") or "")
    saved = _selection(root, selection_id)
    sets = {}
    for path in (root / "selections").glob("*.json"):
        body = read(path)
        sets[str(body.get("selection_id") or path.stem)] = body
    ancestors = []
    parent = saved.get("parent_selection_id")
    while parent:
        body = sets.get(parent)
        ancestors.append(
            {
                "selection_id": parent,
                "name": (body or {}).get("name"),
                "version": (body or {}).get("version") or 1,
                "deleted": body is None,
            }
        )
        parent = (body or {}).get("parent_selection_id")
    children = [
        {"selection_id": key, "name": body.get("name"), "version": body.get("version") or 1}
        for key, body in sets.items()
        if body.get("parent_selection_id") == selection_id
    ]
    jobs = _jobs_by_experiment(root)
    batches = {}
    for path in (
        (root / "replay_batches").glob("*.json") if (root / "replay_batches").is_dir() else []
    ):
        body = read(path)
        for experiment_id in body.get("experiments") or []:
            batches[str(experiment_id)] = body.get("name")
    runs = []
    for path in (root / "experiments").glob("*.json") if (root / "experiments").is_dir() else []:
        experiment = read(path)
        if experiment.get("selection_id") != selection_id:
            continue
        for job in jobs.get(path.stem, []):
            runs.append(
                {
                    **job,
                    "experiment_id": path.stem,
                    "name": batches.get(path.stem) or "Replay",
                    "scenario_name": experiment.get("scenario_name"),
                    "configurations": [
                        item.get("name") for item in experiment.get("configurations") or []
                    ],
                }
            )
    runs.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    job_ids = {run["job_id"] for run in runs}
    candidates = []
    for path in (root / "candidates").glob("*.json") if (root / "candidates").is_dir() else []:
        if path.name.endswith(".freeze.json"):
            continue
        body = read(path)
        if job_ids & set(body.get("result_ids") or []):
            candidates.append(
                {
                    "candidate_id": body.get("candidate_id"),
                    "name": body.get("name"),
                    "frozen": (path.parent / f"{body.get('candidate_id')}.freeze.json").exists(),
                }
            )
    return {
        "selection_id": selection_id,
        "name": saved.get("name"),
        "description": saved.get("description") or "",
        "version": saved.get("version") or 1,
        "ancestors": ancestors,
        "children": children,
        "runs": runs,
        "candidates": candidates,
        "last_used_at": runs[0]["created_at"] if runs else None,
        "profiles": [
            {
                "market_id": row.get("market_id"),
                "window_start_ns": row.get("window_start_ns"),
                "kept": True,
                "outcome": row.get("outcome"),
                "holes": (row.get("book_profile") or {}).get("holes") or [],
                "valid_ratio": (row.get("book_profile") or {}).get("valid_ratio"),
                "longest_hole_s": (row.get("book_profile") or {}).get("longest_hole_s"),
            }
            for row in saved.get("included") or []
        ],
        "has_profiles": any(row.get("book_profile") for row in saved.get("included") or []),
    }


def selection_annotate(request: dict, root: Path) -> dict[str, Any]:
    """Rename or describe a set. Windows, pins and digest are untouched; edits are logged."""
    selection_id = str(request.get("selection_id") or "")
    saved = _selection(root, selection_id)
    changes = {}
    if request.get("name") is not None:
        name = str(request["name"]).strip()
        if not name:
            raise WorkflowError("invalid_name", "a window set needs a name", {"field": "name"})
        changes["name"] = name
    if request.get("description") is not None:
        changes["description"] = str(request["description"]).strip()
    if not changes:
        raise WorkflowError("nothing_to_change", "send a name or a description", {})
    history = list(saved.get("label_history") or [])
    history.append(
        {
            "at": now_iso(),
            "before": {key: saved.get(key) for key in changes},
            "after": changes,
        }
    )
    saved.update(changes)
    saved["label_history"] = history
    write_json(root / "selections" / f"{selection_id}.json", saved)
    return {"selection_id": selection_id, **changes}


EXPORT_FIELDS = (
    "market_id",
    "window_start_utc",
    "window_end_utc",
    "warehouse",
    "collection",
    "block_id",
    "outcome",
    "label_source",
    "up_book_usable_ratio",
    "down_book_usable_ratio",
    "reference_usable_ratio",
    "book_valid_ratio",
    "longest_hole_s",
    "price_to_beat_value",
    "price_to_beat_status",
)


def _utc(ns: Any) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(int(ns) / 1e9, UTC).isoformat() if ns else ""


def selection_export(request: dict, root: Path) -> dict[str, Any]:
    """Membership of a set as CSV or JSON, written under exports/ and returned as text."""
    selection_id = str(request.get("selection_id") or "")
    saved = _selection(root, selection_id)
    fmt = str(request.get("format") or "csv")
    if fmt not in ("csv", "json"):
        raise WorkflowError("invalid_format", "export as csv or json", {"format": fmt})
    rows = []
    for row in saved.get("included") or []:
        profile = row.get("book_profile") or {}
        rows.append(
            {
                "market_id": row.get("market_id"),
                "window_start_utc": _utc(row.get("window_start_ns")),
                "window_end_utc": _utc(row.get("window_end_ns")),
                "warehouse": row.get("warehouse"),
                "collection": row.get("collection"),
                "block_id": row.get("block_id"),
                "outcome": row.get("outcome"),
                "label_source": row.get("label_source"),
                "up_book_usable_ratio": row.get("up_book_usable_ratio"),
                "down_book_usable_ratio": row.get("down_book_usable_ratio"),
                "reference_usable_ratio": row.get("reference_usable_ratio"),
                "book_valid_ratio": profile.get("valid_ratio"),
                "longest_hole_s": profile.get("longest_hole_s"),
                "price_to_beat_value": row.get("price_to_beat_value"),
                "price_to_beat_status": row.get("price_to_beat_status"),
            }
        )
    header = {
        "selection_id": selection_id,
        "name": saved.get("name"),
        "digest": saved.get("digest"),
        "research_policy": saved.get("research_policy"),
        "policy_parameters": saved.get("policy_parameters"),
        "exported_at": now_iso(),
        "windows": len(rows),
    }
    if fmt == "json":
        content = json.dumps({**header, "included": rows}, indent=2, default=str)
        kind = "application/json"
    else:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(EXPORT_FIELDS), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        content = buffer.getvalue()
        kind = "text/csv"
    safe = "".join(
        ch if ch.isalnum() or ch in "-_" else "_" for ch in str(saved.get("name") or "set")
    )
    filename = f"{safe}_{selection_id[:8]}.{fmt}"
    folder = root / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_text(content, encoding="utf-8")
    return {
        "filename": filename,
        "path": str(folder / filename),
        "content_type": kind,
        "content": content,
        "windows": len(rows),
        "header": header,
    }


def reservation_save(request: dict, root: Path) -> dict[str, Any]:
    """Reserve windows (recordings or a time range) as a future Tune holdout."""
    from .inventory import discover
    from .recording import warehouse_roots

    name = str(request.get("name") or "").strip()
    if not name:
        raise WorkflowError("invalid_name", "a reservation needs a name", {"field": "name"})
    block_ids = [str(item) for item in request.get("block_ids") or []]
    start, end = request.get("start_utc"), request.get("end_utc")
    if not block_ids and not (start or end):
        raise WorkflowError("empty_reservation", "choose recordings or a time range to reserve", {})
    scope = {
        "roots": [str(path) for path in warehouse_roots()],
        "limit": 100000,
        **({"block_ids": block_ids} if block_ids else {}),
        **({"start_utc": start} if start else {}),
        **({"end_utc": end} if end else {}),
    }
    rows = [row for row in discover(scope)["windows"] if row.get("block_id")]
    reservation_id = uuid.uuid4().hex
    body = {
        "schema": "workflow_reservation_v1",
        "reservation_id": reservation_id,
        "name": name,
        "purpose": "tune_holdout",
        "created_at": now_iso(),
        "block_ids": block_ids,
        "start_utc": start,
        "end_utc": end,
        "market_ids": sorted({row["market_id"] for row in rows}),
    }
    write_json(root / "reservations" / f"{reservation_id}.json", body)
    return {"reservation_id": reservation_id, "windows": len(body["market_ids"])}


def reservations(root: Path) -> list[dict]:
    folder = root / "reservations"
    return [read(path) for path in sorted(folder.glob("*.json"))] if folder.is_dir() else []


def reservation_list(request: dict, root: Path) -> dict[str, Any]:
    del request
    rows = [
        {key: value for key, value in body.items() if key != "market_ids"}
        | {"windows": len(body.get("market_ids") or [])}
        for body in reservations(root)
    ]
    return {"reservations": rows}


def reservation_delete(request: dict, root: Path) -> dict[str, Any]:
    reservation_id = str(request.get("reservation_id") or "")
    path = root / "reservations" / f"{reservation_id}.json"
    if not reservation_id or not path.is_file():
        raise WorkflowError(
            "not_found", "reservation is not saved", {"reservation_id": reservation_id}
        )
    path.unlink()
    return {"deleted": reservation_id}


def reserved_overlap(root: Path, market_ids: set[str]) -> list[dict[str, Any]]:
    """How many of these windows each reservation holds back for Tune."""
    rows = []
    for body in reservations(root):
        count = len(market_ids & set(body.get("market_ids") or []))
        if count:
            rows.append(
                {"reservation_id": body["reservation_id"], "name": body["name"], "windows": count}
            )
    return rows
