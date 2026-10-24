"""Library, preparation, and pinned selections."""

from __future__ import annotations

import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from tyrex_pm.data_warehouse.capture.files import read_json

from .artifacts import file_sha256, now_iso, plan_digest, read, slice_page, write_json
from .contracts import WorkflowError


def _roots(request: dict) -> list[Path]:
    raw = request.get("roots") or []
    if not raw:
        raise WorkflowError("missing_field", "roots are required", {"field": "roots"})
    return [Path(item) for item in raw]


def library_collections(request: dict, root: Path) -> dict[str, Any]:
    found = []
    for warehouse in _roots(request):
        collections = warehouse / "collections"
        if not collections.is_dir():
            continue
        for path in sorted(item for item in collections.iterdir() if item.is_dir()):
            meta_path = path / "collection.json"
            if not meta_path.exists():
                continue
            meta = read_json(meta_path)
            sessions = []
            session_dir = path / "sessions"
            if session_dir.is_dir():
                for session_path in sorted(session_dir.glob("*.json")):
                    session = read_json(session_path)
                    sessions.append(
                        {
                            "session_id": session.get("session_id") or session_path.stem,
                            "started_at": session.get("started_at"),
                            "stopped_at": session.get("stopped_at"),
                            "clean_stop": session.get("clean_stop"),
                            "generation": session.get("generation"),
                        }
                    )
            found.append(
                {
                    "warehouse": str(warehouse),
                    "collection": path.name,
                    "profile_hash": (meta.get("profile") or {}).get("hash")
                    or meta.get("profile_hash"),
                    "sessions": sessions,
                }
            )
    collections, next_cursor = slice_page(found, request)
    return {
        "collections": collections,
        "next_cursor": next_cursor,
        "coverage_spans_are_sessions": False,
    }


def library_inspect(request: dict, root: Path) -> dict[str, Any]:
    selection_id = request.get("selection_id")
    if not selection_id:
        raise WorkflowError("missing_field", "selection_id is required", {"field": "selection_id"})
    path = root / "selections" / f"{selection_id}.json"
    if not path.exists():
        raise WorkflowError("not_found", "selection is not saved", {"selection_id": selection_id})
    saved = read(path)
    return {
        "selection_id": selection_id,
        "included": len(saved["included"]),
        "excluded": saved["excluded"],
        "windows": [
            {
                "market_id": row["market_id"],
                "collection": row["collection"],
                "reference_series": row.get("reference_series"),
                "capture_id": row.get("capture_id"),
            }
            for row in saved["included"]
        ],
    }


def library_prepare(request: dict, root: Path) -> dict[str, Any]:
    kind = request.get("kind")
    warehouse = request.get("warehouse")
    collection = request.get("collection")
    if kind not in {"normalize", "quality", "verify"}:
        raise WorkflowError(
            "preparation_kind",
            "preparation runs normalize, quality, or verify on data already recorded",
            {"kind": kind},
        )
    if not warehouse or not collection:
        raise WorkflowError(
            "missing_field",
            "warehouse and collection are required",
            {"field": "warehouse"},
        )
    warehouse_path = Path(warehouse)
    try:
        if kind == "normalize":
            from tyrex_pm.data_warehouse.normalize.worker import normalize_once

            result = normalize_once(warehouse_path, collection)
        elif kind == "quality":
            from tyrex_pm.data_warehouse.quality.publish import publish_quality

            result = publish_quality(warehouse_path, collection)
        else:
            from tyrex_pm.data_warehouse.query.export import verify

            result = verify(warehouse_path, collection)
    except Exception as exc:  # noqa: BLE001 - owner errors become structured blockers
        raise WorkflowError(
            "preparation_failed",
            str(exc),
            {"kind": kind, "error_type": type(exc).__name__},
        ) from exc
    return {
        "kind": kind,
        "warehouse": str(warehouse_path),
        "collection": collection,
        "owner_result": result,
        "collects_new_observations": False,
    }


def _groups(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row["market_id"])].append(row)
    return grouped


def _source_kind(item: dict) -> str:
    return str(item.get("source_kind") or "collection_recording")


def selection_resolve(request: dict, root: Path) -> dict[str, Any]:
    rows = list(request.get("windows") or [])
    choices = dict(request.get("choices") or {})
    series_choices = dict((request.get("reference_series_policy") or {}).get("choices") or {})
    capture_choices = dict(request.get("capture_choices") or {})
    included: list[dict] = []
    excluded: list[dict] = []
    for market_id, items in _groups(rows).items():
        kinds = {_source_kind(item) for item in items}
        if "trading_capture" in kinds and kinds != {"trading_capture"}:
            chosen_kind = capture_choices.get(market_id)
            if chosen_kind not in kinds:
                excluded.append(
                    {
                        "market_id": market_id,
                        "reason": "capture_provenance_required",
                        "alternatives": [
                            {
                                "source_kind": _source_kind(item),
                                "collection": item.get("collection"),
                            }
                            for item in items
                        ],
                    }
                )
                continue
            items = [item for item in items if _source_kind(item) == chosen_kind]
        series = {item.get("reference_series") for item in items}
        collections = {item["collection"] for item in items}
        if len(collections) == 1 and len(series) > 1:
            chosen_series = series_choices.get(market_id)
            if chosen_series not in series:
                excluded.append(
                    {
                        "market_id": market_id,
                        "reason": "ambiguous_reference_series",
                        "alternatives": [
                            {"reference_series": item.get("reference_series")} for item in items
                        ],
                    }
                )
                continue
            items = [item for item in items if item.get("reference_series") == chosen_series]
        if len(items) == 1:
            included.append(dict(items[0]))
            continue
        choice = choices.get(market_id)
        alternatives = [
            {"collection": item["collection"], "warehouse": item.get("warehouse")} for item in items
        ]
        if not choice:
            excluded.append(
                {
                    "market_id": market_id,
                    "reason": "ambiguous_duplicate",
                    "alternatives": alternatives,
                }
            )
            continue
        matched = [item for item in items if item["collection"] == choice]
        if len(matched) != 1:
            excluded.append(
                {
                    "market_id": market_id,
                    "reason": "unresolved_choice",
                    "alternatives": alternatives,
                }
            )
            continue
        chosen = dict(matched[0])
        included.append(chosen)
        excluded.append(
            {
                "market_id": market_id,
                "reason": "alternate_recording",
                "kept_collection": choice,
                "alternatives": [item for item in alternatives if item["collection"] != choice],
            }
        )
    return {
        "included": included,
        "excluded": excluded,
        "requires_choice": [
            row["market_id"]
            for row in excluded
            if row["reason"]
            in {
                "ambiguous_duplicate",
                "ambiguous_reference_series",
                "capture_provenance_required",
            }
        ],
    }


def _pin_manifest() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "parser_version": "1",
        "build_id": "workflow_pin",
        "first_received_at": 0,
        "last_received_at": 0,
        "files": [],
    }


def selection_save(request: dict, root: Path) -> dict[str, Any]:
    if request.get("preview_id"):
        from .recipes import save_from_preview

        return save_from_preview(request, root)
    resolved = selection_resolve(request, root)
    if resolved["requires_choice"]:
        pending = [
            row for row in resolved["excluded"] if row["market_id"] in resolved["requires_choice"]
        ]
        raise WorkflowError(
            pending[0]["reason"],
            "an explicit source choice is required before this selection can be saved",
            {"market_ids": resolved["requires_choice"]},
        )
    parts = []
    for item in request.get("pin_parts") or []:
        path = Path(item["path"])
        if not path.is_file():
            raise WorkflowError("missing_part", "pinned part is missing", {"path": str(path)})
        parts.append({"path": str(path.resolve()), "sha256": file_sha256(path)})
    body = {
        "schema": "workflow_selection_v1",
        "selection_id": request.get("selection_id") or uuid.uuid4().hex,
        "name": request.get("name") or "",
        "intended_use": request.get("intended_use") or "development",
        "created_at": now_iso(),
        "included": resolved["included"],
        "excluded": resolved["excluded"],
        "reference_series_policy": request.get("reference_series_policy") or {},
        "capture_provenance": request.get("capture_provenance") or [],
        "pin_parts": parts,
        "manifests": [_pin_manifest()],
        "warehouse": request.get("warehouse"),
        "collection": request.get("collection"),
    }
    body["digest"] = plan_digest(
        {
            key: body[key]
            for key in (
                "included",
                "pin_parts",
                "manifests",
                "intended_use",
                "reference_series_policy",
                "capture_provenance",
            )
        }
    )
    path = root / "selections" / f"{body['selection_id']}.json"
    if path.exists():
        raise WorkflowError(
            "selection_exists",
            "a saved selection is immutable; create a new id",
            {"selection_id": body["selection_id"]},
        )
    write_json(path, body)
    return {
        "selection_id": body["selection_id"],
        "digest": body["digest"],
        "included": len(body["included"]),
        "excluded": len(body["excluded"]),
        "path": str(path),
    }


def _verify_parts(parts: list[dict]) -> None:
    for item in parts:
        path = Path(item["path"])
        if not path.is_file():
            raise WorkflowError("missing_part", "pinned part is missing", {"path": str(path)})
        current = file_sha256(path)
        if current != item["sha256"]:
            raise WorkflowError(
                "pin_mismatch",
                "pinned bytes changed; latest data was not substituted",
                {"path": str(path), "expected": item["sha256"], "actual": current},
            )


def selection_open(request: dict, root: Path) -> dict[str, Any]:
    selection_id = request.get("selection_id")
    path = root / "selections" / f"{selection_id}.json"
    if not path.exists():
        raise WorkflowError("not_found", "selection is not saved", {"selection_id": selection_id})
    saved = read(path)
    from .recipes import _compatibility, _holdout_overlap, _legacy, verify_saved_catalog

    legacy = _legacy(saved)
    if legacy:
        _verify_parts(saved.get("pin_parts") or [])
        catalog_windows = None
    else:
        catalog_windows = verify_saved_catalog(saved)
    return {
        "selection_id": selection_id,
        "name": saved.get("name") or "",
        "digest": saved["digest"],
        "included": saved["included"],
        "excluded": saved["excluded"],
        "catalog_windows": catalog_windows,
        "filters": saved.get("filters"),
        "recipe": saved.get("recipe"),
        "cookbook": saved.get("cookbook"),
        "population": saved.get("population"),
        "intended_use": saved.get("intended_use"),
        "outcome_conditioned": bool(saved.get("outcome_conditioned")),
        "preview_id": saved.get("preview_id"),
        "reference_series_policy": saved.get("reference_series_policy") or {},
        "capture_provenance": saved.get("capture_provenance") or [],
        "limitation": "legacy_unpinned" if legacy else None,
        "block_ids": saved.get("block_ids") or [],
        "requirements": saved.get("requirements") or {},
        "coverage_floor": saved.get("coverage_floor"),
        "settlement_confidence": saved.get("settlement_confidence"),
        "research_policy": saved.get("research_policy"),
        "research_limitation": saved.get("research_limitation"),
        "start_utc": saved.get("start_utc") or "",
        "end_utc": saved.get("end_utc") or "",
        "holdout_overlap": _holdout_overlap(saved),
        "compatibility": _compatibility(saved),
    }
