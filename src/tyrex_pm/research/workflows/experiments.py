"""Settlement experiment resolution and preview. No lab defaults are merged."""

from __future__ import annotations

import itertools
import uuid
from pathlib import Path
from typing import Any

from tyrex_pm.strategies.settlement_value.schema import (
    settlement_value_config_as_parameters,
    settlement_value_config_from_parameters,
)

from .artifacts import now_iso, plan_digest, read, source_fingerprint, write_json
from .contracts import WorkflowError

DEFAULT_MAX_UNITS = 64


def _configurations(request: dict) -> tuple[list[dict], int]:
    if "grid" in request:
        axes = request["grid"].get("axes") or {}
        if not axes:
            raise WorkflowError(
                "empty_grid", "a grid needs at least one axis", {"field": "grid.axes"}
            )
        names = list(axes)
        lengths = [len(list(axes[name])) for name in names]
        count = 1
        for length in lengths:
            count *= length
        return [{"axes": names, "count": count}], count
    if request.get("configurations"):
        configs = [dict(item) for item in request["configurations"]]
        return configs, len(configs)
    return [dict(request.get("parameters") or {})], 1


def _expand(request: dict) -> list[dict]:
    if "grid" not in request:
        configs, _ = _configurations(request)
        return configs
    axes = request["grid"]["axes"]
    names = list(axes)
    expanded = []
    for values in itertools.product(*(axes[name] for name in names)):
        parameters = dict(request.get("parameters") or {})
        for name, value in zip(names, values, strict=True):
            section, _, field = name.partition(".")
            if not field:
                raise WorkflowError(
                    "grid_axis",
                    "grid axes use section.field names",
                    {"axis": name},
                )
            parameters.setdefault(section, {})
            parameters[section][field] = value
        expanded.append(parameters)
    return expanded


def _unit_count(request: dict) -> int:
    windows = list(request.get("windows") or [])
    scenarios = list(request.get("scenarios") or ["favorable"])
    _, configs = _configurations(request)
    return len(windows) * configs * len(scenarios)


def experiment_resolve(request: dict, root: Path) -> dict[str, Any]:
    if request.get("research_defaults"):
        raise WorkflowError(
            "research_defaults_refused",
            "lab research defaults are not merged into a workflow experiment",
            {"field": "research_defaults"},
        )
    if request.get("strategy_kind") not in (None, "settlement_value"):
        raise WorkflowError(
            "unsupported_strategy",
            "the workflow replay adapter is settlement_value",
            {"strategy_kind": request.get("strategy_kind")},
        )
    limit = int(request.get("max_units") or DEFAULT_MAX_UNITS)
    planned = _unit_count(request)
    if planned > limit:
        raise WorkflowError(
            "resource_limit",
            "planned units exceed max_units; the grid was not expanded",
            {"planned": planned, "max_units": limit},
        )
    if planned < 1:
        raise WorkflowError("empty_plan", "windows and a configuration are required", {})
    configs = _expand(request)
    resolved = []
    for parameters in configs:
        try:
            loaded = settlement_value_config_from_parameters(parameters, file="workflow")
        except Exception as exc:  # noqa: BLE001 - schema errors are structured refusals
            raise WorkflowError(
                "invalid_configuration",
                str(exc),
                {"error_type": type(exc).__name__},
            ) from exc
        resolved.append(settlement_value_config_as_parameters(loaded, full=True))
    return {
        "strategy_kind": "settlement_value",
        "configurations": resolved,
        "windows": list(request.get("windows") or []),
        "scenarios": list(request.get("scenarios") or ["favorable"]),
        "max_units": limit,
        "planned_units": planned,
        "source_fingerprint": source_fingerprint(),
        "merged_research_defaults": False,
    }


def experiment_save(request: dict, root: Path) -> dict[str, Any]:
    resolved = experiment_resolve(request, root)
    experiment_id = request.get("experiment_id") or uuid.uuid4().hex
    path = root / "experiments" / f"{experiment_id}.json"
    if path.exists():
        raise WorkflowError(
            "experiment_exists",
            "a saved experiment is immutable",
            {"experiment_id": experiment_id},
        )
    units = list(request.get("units") or [])
    body = {
        "schema": "workflow_experiment_v1",
        "experiment_id": experiment_id,
        "created_at": now_iso(),
        "resolved": resolved,
        "units": units,
        "selection_id": request.get("selection_id"),
        "pins": request.get("pins") or {},
    }
    body["units_source"] = "caller"
    body["digest"] = plan_digest({"resolved": resolved, "units": units, "pins": body["pins"]})
    write_json(path, body)
    return {
        "experiment_id": experiment_id,
        "digest": body["digest"],
        "planned_units": resolved["planned_units"],
    }


def experiment_bind(request: dict, root: Path) -> dict[str, Any]:
    if request.get("units") or request.get("pin_parts"):
        raise WorkflowError(
            "units_refused",
            "the browser does not assemble replay units or pins",
            {"fields": [name for name in ("units", "pin_parts") if request.get(name)]},
        )
    selection_id = request.get("selection_id")
    if not selection_id:
        raise WorkflowError("missing_field", "selection_id is required", {"field": "selection_id"})
    from .recipes import _legacy, saved_selection, verify_saved_catalog

    saved = saved_selection(root, selection_id)
    if _legacy(saved):
        raise WorkflowError(
            "legacy_unpinned",
            "this selection has no warehouse pin and cannot be bound",
            {"selection_id": selection_id},
        )
    verify_saved_catalog(saved)
    if saved.get("recipe") == "missing_inputs":
        raise WorkflowError(
            "diagnostic_not_replayable",
            "a missing-input diagnostic set cannot open an ordinary replay",
            {"selection_id": selection_id},
        )
    if request.get("strategy_kind") not in (None, "settlement_value"):
        raise WorkflowError(
            "unsupported_strategy",
            "the workflow replay adapter is settlement_value",
            {"strategy_kind": request.get("strategy_kind")},
        )
    metrics = list(request.get("metrics") or [])
    required = {"name", "unit", "denominator", "eligible_population", "missing_value_rule"}
    for metric in metrics:
        missing = required - set(metric)
        if missing:
            raise WorkflowError(
                "missing_metric_definition",
                "each metric names its unit, denominator, population, and missing-value rule",
                {"missing": sorted(missing)},
            )
    if not metrics:
        raise WorkflowError("missing_metric_definition", "metric definitions are required", {})
    windows = [
        {
            "market_id": row["market_id"],
            "collection": row["collection"],
            "warehouse": row["warehouse"],
            "block_id": row.get("block_id"),
        }
        for row in saved["included"]
    ]
    parameters = dict(request.get("parameters") or {})
    if saved.get("research_policy") == "exploratory":
        quality = dict(parameters.get("quality") or {})
        accepted = [str(item) for item in quality.get("accepted_k_qualities") or []]
        if not accepted:
            accepted = ["CONFIRMED_CANONICAL", "PROVISIONAL"]
        elif "PROVISIONAL" not in accepted:
            accepted.append("PROVISIONAL")
        quality["accepted_k_qualities"] = accepted
        parameters["quality"] = quality
    resolve_request = {
        "windows": windows,
        "parameters": parameters,
        "configurations": request.get("configurations"),
        "scenarios": request.get("scenarios") or ["friction"],
        "max_units": request.get("max_units") or DEFAULT_MAX_UNITS,
        "strategy_kind": "settlement_value",
    }
    if request.get("grid"):
        resolve_request["grid"] = request["grid"]
    resolved = experiment_resolve(resolve_request, root)
    repo = Path(__file__).resolve().parents[4]
    scenario_names = list(resolve_request["scenarios"])
    scenario_paths = []
    for name in scenario_names:
        path = repo / "config" / "research" / "scenarios" / f"{name}.yaml"
        if not path.is_file():
            raise WorkflowError(
                "unknown_scenario", "execution scenario is not on disk", {"scenario": name}
            )
        scenario_paths.append(str(path))
    units = []
    for window in windows:
        for index, parameters in enumerate(resolved["configurations"]):
            for scenario_name, scenario_path in zip(scenario_names, scenario_paths, strict=True):
                units.append(
                    {
                        "id": f"{window['market_id']}-{index}-{scenario_name}",
                        "kind": "strategy_replay",
                        "market_id": window["market_id"],
                        "warehouse": window["warehouse"],
                        "collection": window["collection"],
                        "block_id": window.get("block_id"),
                        "scenario_path": scenario_path,
                        "scenario_name": scenario_name,
                        "parameters": parameters,
                    }
                )
    if len(units) != resolved["planned_units"]:
        raise WorkflowError("empty_plan", "unit expansion did not match the planned count", {})
    experiment_id = request.get("experiment_id") or uuid.uuid4().hex
    path = root / "experiments" / f"{experiment_id}.json"
    if path.exists():
        raise WorkflowError(
            "experiment_exists", "a saved experiment is immutable", {"experiment_id": experiment_id}
        )
    population = saved.get("population") or "unconditioned"
    limitation = saved.get("research_limitation")
    if saved.get("outcome_conditioned"):
        limitation = (
            f"{limitation} Outcome-conditioned diagnostic." if limitation else "outcome_conditioned"
        )
    grouped_units: dict[str, list[str]] = {}
    for unit in units:
        grouped_units.setdefault(str(unit.get("block_id") or "unassigned"), []).append(unit["id"])
    block_jobs = [
        {"block_id": block_id, "units": len(unit_ids)}
        for block_id, unit_ids in grouped_units.items()
    ]
    body = {
        "schema": "workflow_experiment_v1",
        "experiment_id": experiment_id,
        "created_at": now_iso(),
        "units_source": "selection",
        "selection_id": selection_id,
        "selection_digest": saved.get("digest"),
        "population": population,
        "outcome_conditioned": bool(saved.get("outcome_conditioned")),
        "intended_use": saved.get("intended_use"),
        "baseline": request.get("baseline"),
        "metrics": metrics,
        "acceptance_criteria": request.get("acceptance_criteria") or "",
        "resolved": resolved,
        "units": units,
        "pins": {"manifests": saved.get("manifests") or [], "preview_id": saved.get("preview_id")},
        "limitation": limitation,
        "block_jobs": block_jobs,
        "research_policy": saved.get("research_policy"),
        "k_acceptance": list(
            (resolved["configurations"][0].get("quality") or {}).get("accepted_k_qualities") or []
        ),
    }
    body["digest"] = plan_digest(
        {
            "selection_id": selection_id,
            "selection_digest": saved.get("digest"),
            "units": units,
            "metrics": metrics,
            "population": population,
            "baseline": request.get("baseline"),
        }
    )
    write_json(path, body)
    return {
        "experiment_id": experiment_id,
        "digest": body["digest"],
        "planned_units": resolved["planned_units"],
        "units_source": "selection",
        "population": population,
        "outcome_conditioned": bool(saved.get("outcome_conditioned")),
        "limitation": body["limitation"],
        "block_jobs": block_jobs,
        "research_policy": saved.get("research_policy"),
        "k_acceptance": body["k_acceptance"],
        "selection_id": selection_id,
        "estimate": None,
        "estimate_reason": "no_measured_sample",
    }


def experiment_list(request: dict, root: Path) -> dict[str, Any]:
    folder = root / "experiments"
    rows = []
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            saved = read(path)
            rows.append(
                {
                    "experiment_id": saved.get("experiment_id") or path.stem,
                    "selection_id": saved.get("selection_id"),
                    "units_source": saved.get("units_source") or "caller",
                    "population": saved.get("population"),
                    "outcome_conditioned": saved.get("outcome_conditioned"),
                    "limitation": saved.get("limitation"),
                    "planned_units": (saved.get("resolved") or {}).get("planned_units"),
                    "created_at": saved.get("created_at"),
                }
            )
    from .artifacts import slice_page

    page, cursor = slice_page(rows, request)
    return {"experiments": page, "next_cursor": cursor}


def _verified_completed(job_dir: Path, units: list[dict]) -> int:
    manifest_path = job_dir / "parts" / "manifest.json"
    if not manifest_path.exists():
        return 0
    manifest = read(manifest_path)
    from .jobs import part_path

    done = 0
    for unit in units:
        entry = (manifest.get("parts") or {}).get(unit["id"])
        target = part_path(job_dir / "parts", unit["id"])
        if not entry or not target.is_file():
            continue
        from .artifacts import file_sha256

        if file_sha256(target) == entry["sha256"]:
            done += 1
    return done


def experiment_preview(request: dict, root: Path) -> dict[str, Any]:
    experiment_id = request.get("experiment_id")
    path = root / "experiments" / f"{experiment_id}.json"
    if not path.exists():
        raise WorkflowError(
            "not_found", "experiment is not saved", {"experiment_id": experiment_id}
        )
    saved = read(path)
    if request.get("digest") and request["digest"] != saved["digest"]:
        raise WorkflowError(
            "identity_mismatch",
            "preview digest does not match the saved experiment",
            {"field": "digest", "expected": saved["digest"], "actual": request["digest"]},
        )
    resolved = saved["resolved"]
    units = saved.get("units") or []
    completed = 0
    job_id = request.get("job_id")
    if job_id and units:
        completed = _verified_completed(root / "jobs" / job_id, units)
    return {
        "experiment_id": experiment_id,
        "digest": saved["digest"],
        "windows": len(resolved["windows"]),
        "configurations": len(resolved["configurations"]),
        "scenarios": len(resolved["scenarios"]),
        "units": resolved["planned_units"],
        "completed": completed,
        "remaining": resolved["planned_units"] - completed,
        "estimate": None,
        "estimate_reason": "no_measured_sample",
        "exclusions": [],
    }
