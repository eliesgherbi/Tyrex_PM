"""Candidate-to-paper translation. Research defaults are not copied in."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from tyrex_pm.runtime.run_config import RunConfigError, load_trading_run_config
from tyrex_pm.strategies.ask70.schema import ask70_config_from_parameters
from tyrex_pm.strategies.settlement_value.schema import settlement_value_config_from_parameters

from .artifacts import now_iso, plan_digest, read, write_json
from .contracts import WorkflowError

OPERATIONAL_FIELDS = (
    "venue",
    "paper",
    "state_directory",
    "report_directory",
    "market",
    "account",
    "risk",
    "lifecycle",
)


def _candidate(root: Path, candidate_id: str) -> dict:
    path = root / "candidates" / f"{candidate_id}.json"
    if not path.exists():
        raise WorkflowError("not_found", "candidate is not saved", {"candidate_id": candidate_id})
    return read(path)


def _require_operational(operational: dict, kind: str) -> None:
    missing = [name for name in OPERATIONAL_FIELDS if name not in operational]
    if kind == "settlement_value" and "settlement" not in operational:
        missing.append("settlement")
    if missing:
        raise WorkflowError(
            "missing_operational_field",
            "a runnable profile needs explicit operational fields",
            {"fields": missing},
        )
    if operational.get("venue") != "paper":
        raise WorkflowError(
            "venue", "workflow profiles are paper profiles", {"venue": operational.get("venue")}
        )
    if operational.get("research_defaults"):
        raise WorkflowError(
            "research_defaults_refused",
            "provisional research tolerances are not inherited",
            {"field": "research_defaults"},
        )


def _check_kind(kind: str, parameters: dict, operational: dict) -> None:
    if kind == "settlement_value":
        settlement_value_config_from_parameters(parameters, file="workflow")
        mode = (operational.get("lifecycle") or {}).get("mode")
        if mode not in {"hold_to_resolution", "managed_exit"}:
            raise WorkflowError(
                "lifecycle",
                "settlement paper profiles name hold_to_resolution or managed_exit",
                {"lifecycle.mode": mode},
            )
        return
    if kind == "ask70":
        ask70_config_from_parameters(parameters, file="workflow")
        mode = (operational.get("lifecycle") or {}).get("mode", "flatten_before_close")
        run_mode = (operational.get("market") or {}).get("run_mode", "single_window")
        if mode != "flatten_before_close" or run_mode != "single_window":
            raise WorkflowError(
                "ask70_lifecycle",
                "ask70 paper profiles use flatten_before_close and single_window",
                {"lifecycle.mode": mode, "market.run_mode": run_mode},
            )
        if "protection" not in operational:
            raise WorkflowError(
                "missing_operational_field", "ask70 requires protection", {"fields": ["protection"]}
            )
        return
    raise WorkflowError(
        "unsupported_strategy",
        "this profile generator does not support that kind",
        {"strategy_kind": kind},
    )


def profile_generate(request: dict, root: Path) -> dict[str, Any]:
    candidate = _candidate(root, request["candidate_id"])
    operational = dict(request.get("operational") or {})
    dropped = operational.pop("strategy", None)
    kind = candidate["strategy_kind"]
    parameters = candidate["parameters"]
    _require_operational(operational, kind)
    _check_kind(kind, parameters, operational)
    output_dir = Path(request["output_directory"])
    if output_dir.name != "generated":
        raise WorkflowError(
            "output_directory",
            "generated profiles stay in a generated directory",
            {"output_directory": str(output_dir)},
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    digest = plan_digest(
        {"candidate": candidate["digest"], "operational": operational, "parameters": parameters}
    )
    target = output_dir / f"{candidate['candidate_id']}-{digest[:12]}.yaml"
    if target.exists():
        raise WorkflowError(
            "profile_exists", "generated profiles are not overwritten", {"path": str(target)}
        )
    document = {
        "schema_version": 1,
        "run_name": request.get("run_name") or f"workflow_{candidate['candidate_id'][:12]}",
        "venue": "paper",
        "strategy": {"kind": kind, "parameters": parameters},
        **operational,
    }
    target.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    try:
        load_trading_run_config(target)
    except RunConfigError as exc:
        target.unlink(missing_ok=True)
        raise WorkflowError("profile_invalid", str(exc), {"field": "profile"}) from exc
    report = {
        "schema": "workflow_translation_v1",
        "candidate_id": candidate["candidate_id"],
        "candidate_digest": candidate["digest"],
        "yaml_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "path": str(target),
        "created_at": now_iso(),
        "preserved": {"strategy.kind": kind, "strategy.parameters": parameters},
        "omitted": {
            "research_defaults": "not merged",
            "request_strategy": dropped is not None,
        },
        "added": sorted(
            name for name in document if name not in {"strategy", "schema_version", "run_name"}
        ),
        "unresolved": ["step2_follow_up_open", "paper_launch_not_qualified"],
        "launch_allowed": False,
    }
    report_path = root / "translations" / f"{candidate['candidate_id']}-{digest[:12]}.json"
    write_json(report_path, report)
    return {
        "path": str(target),
        "translation": str(report_path),
        "yaml_sha256": report["yaml_sha256"],
        "launch_allowed": False,
    }


def profile_validate(request: dict, root: Path) -> dict[str, Any]:
    path = Path(request["path"])
    if not path.is_file():
        raise WorkflowError("not_found", "profile file is missing", {"path": str(path)})
    try:
        config = load_trading_run_config(path)
    except RunConfigError as exc:
        raise WorkflowError("profile_invalid", str(exc), {"path": str(path)}) from exc
    return {
        "path": str(path),
        "strategy_kind": config.strategy_kind,
        "venue": config.venue,
        "run_mode": config.market.run_mode,
        "lifecycle_mode": config.lifecycle_mode,
        "schema_valid": True,
        "qualified": False,
    }
