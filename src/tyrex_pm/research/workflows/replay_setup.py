"""Replay setup for the integrated workbench.

The browser sends a saved selection and configuration values. This module resolves
the effective configuration, checks the visible research policy, and builds replay
units. It does not apply a development/holdout split and it does not merge policy
fields that the caller omitted.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from tyrex_pm.data_warehouse.config import strict
from tyrex_pm.research.replay.config import Scenario
from tyrex_pm.runtime.config_error import ConfigError
from tyrex_pm.strategies.settlement_value.config import (
    COMPARISONS,
    K_QUALITIES,
    PROVENANCE_STATUSES,
    SPOT_INPUTS,
    SUPPORTED_BACKENDS,
    TARGET_MODES,
)
from tyrex_pm.strategies.settlement_value.schema import (
    settlement_value_config_as_parameters,
    settlement_value_config_from_parameters,
)

from .artifacts import REPO, now_iso, plan_digest, read, write_json
from .contracts import WorkflowError

ESSENTIALS = (
    "entry.min_edge_per_share",
    "entry.tau_min_s",
    "entry.tau_max_s",
    "sizing.max_debit_usdc",
    "entry.max_entry_price",
    "exit.policy",
)
POLICY_FIELDS = {
    "quality.accepted_k_qualities": ["CONFIRMED_CANONICAL", "PROVISIONAL"],
    "quality.require_verified_settlement_spec": False,
}
GROUPS = (
    {
        "id": "entry",
        "name": "Entry & position size",
        "description": "When to enter, price limits, spending and order sizing.",
        "sections": ["entry", "sizing"],
    },
    {
        "id": "model",
        "name": "Model & volatility",
        "description": "Probability inputs, uncertainty bands and volatility estimator.",
        "sections": ["model", "volatility"],
    },
    {
        "id": "exit",
        "name": "Exit, retry & re-entry",
        "description": "Lifecycle choices and bounded recovery of orders and positions.",
        "sections": ["exit", "retry", "reentry"],
    },
    {
        "id": "quality",
        "name": "Settlement & research policy",
        "description": "Payoff interpretation, input quality and the visible set policy.",
        "sections": ["settlement", "quality"],
    },
    {
        "id": "fees",
        "name": "Strategy fee estimates",
        "description": "Strategy fee curve. Simulation fees stay with the execution scenario.",
        "sections": ["fees"],
    },
    {
        "id": "veto",
        "name": "Model / market disagreement",
        "description": "Optional entry veto. It does not alter an existing hold or exit.",
        "sections": ["disagreement_veto"],
    },
)
CHOICES = {
    "model.backend": sorted(SUPPORTED_BACKENDS),
    "model.spot_input": sorted(SPOT_INPUTS),
    "settlement.comparison": sorted(COMPARISONS),
    "settlement.provenance_status": sorted(PROVENANCE_STATUSES),
    "entry.target_mode": sorted(TARGET_MODES),
    "quality.accepted_k_qualities": sorted(K_QUALITIES),
    "exit.policy": ["hold_to_resolution", "managed_exit"],
}
LABELS = {
    "entry.min_edge_per_share": "Minimum conservative edge",
    "entry.tau_min_s": "Stop new entries this close to expiry",
    "entry.tau_max_s": "Start entries with this much time left",
    "sizing.max_debit_usdc": "Maximum acquisition spending",
    "entry.max_entry_price": "Maximum entry price",
    "exit.policy": "Position lifecycle",
    "quality.accepted_k_qualities": "Accepted price-to-beat quality",
    "quality.require_verified_settlement_spec": "Require verified settlement specification",
}
LABELS.update(
    {
        "model.backend": "Probability model",
        "volatility.half_life_s": "Volatility half-life",
        "exit.value_exit_enabled": "Enable value exit",
        "retry.enabled": "Allow bounded entry retry",
        "reentry.max_trades_per_window": "Maximum trades per window",
    }
)
EXECUTION_LABELS = {
    "order_delay_ms": "Order arrival delay",
    "feed_delay_ms": "Market-data delay",
    "decision_delay_ms": "Decision delay",
    "cancel_delay_ms": "Cancel delay",
    "feedback_delay_ms": "Fill feedback delay",
    "participation": "Available displayed depth",
    "adverse_price_ticks": "Adverse price penalty",
    "timer_interval_ms": "Strategy timer interval",
    "initial_cash": "Initial simulated cash",
    "settlement_release_delay_ms": "Settlement release delay",
    "fee_policy.rate": "Simulated fee rate",
}


def field_label(key: str) -> str:
    known = LABELS.get(key) or EXECUTION_LABELS.get(key)
    if known:
        return known
    name = key.split(".")[-1].replace("_", " ")
    return name[:1].upper() + name[1:]


def field_unit(key: str, kind: str = "", integer: bool = False) -> str:
    name = key.split(".")[-1]
    if name.endswith("_ms"):
        return "ms"
    if name.endswith("_s"):
        return "seconds"
    if "usdc" in name or name == "initial_cash":
        return "USDC"
    if "per_share" in name or name.endswith("price"):
        return "USDC / share"
    if name.endswith("_bps"):
        return "basis points"
    if name.endswith("_pp"):
        return "percentage points"
    if name.endswith("ticks"):
        return "ticks"
    if name == "participation":
        return "share of displayed depth"
    if kind == "list":
        return "Comma-separated values"
    if integer:
        return "Whole number"
    return ""


# The first scenario is the default. Legacy scenarios stay selectable to reproduce
# results produced before 23 October 2026.
SCENARIO_NAMES = (
    "friction_v3",
    "friction_stress_v2",
    "friction_v2",
    "friction_stress_v1",
    "friction",
    "favorable",
)
DEFAULT_SCENARIO = SCENARIO_NAMES[0]
SCENARIO_LABELS = {
    "friction_v3": "Friction v3 · base research assumptions, venue-reconciled books",
    "friction_stress_v2": "Friction stress v2 · book one tick worse, thinner, slower",
    "friction_v2": "Friction v2 (previous) · legacy book rebuild",
    "friction_stress_v1": "Friction stress v1 (previous) · legacy book rebuild",
    "friction": "Friction (legacy) · extra tick excludes the best ask",
    "favorable": "Favorable (legacy) · generous fill assumptions",
}
# Research policies a saved set may carry. Both accept the replay K (exact start tick,
# not attested) as an explicit, visible configuration value.
REPLAY_POLICIES = {
    "replay_research_v2_1": {
        "name": "replay_research_v2_1",
        "description": (
            "Both books valid ≥ 90% of the window, no hole longer than 20 s, none in the "
            "last 60 s; blips under 250 ms ignored. K from the exact start tick (not "
            "attested). Provider-reported outcome. $1 / $0 payout."
        ),
    },
    "replay_research_v2": {
        "name": "replay_research_v2",
        "description": (
            "Both books valid ≥ 90% of the window, no hole longer than 30 s, none in the "
            "last 60 s; blips under 250 ms ignored. K from the exact start tick (not "
            "attested). Provider-reported outcome. $1 / $0 payout."
        ),
    },
    "exploratory": {
        "name": "exploratory95_v1",
        "description": (
            "Each book valid ≥ 95% of the window. Provider-reported outcome. "
            "Provisional K accepted. $1 / $0 payout."
        ),
    },
}


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def _field_type(value: Any) -> tuple[str, bool]:
    if isinstance(value, bool):
        return "boolean", False
    if isinstance(value, int):
        return "number", True
    if isinstance(value, (float, Decimal)):
        return "number", False
    if isinstance(value, tuple):
        return "list", False
    return "text", False


def _flatten(value: dict, prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, dict):
            flat.update(_flatten(item, path))
        else:
            flat[path] = _plain(item)
    return flat


def _unflatten(flat: dict[str, Any]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for key, value in flat.items():
        cursor = root
        parts = str(key).split(".")
        for part in parts[:-1]:
            current = cursor.get(part)
            if not isinstance(current, dict):
                current = {}
                cursor[part] = current
            cursor = current
        cursor[parts[-1]] = value
    return root


def _scenario_path(name: str) -> Path:
    path = REPO / "config" / "research" / "scenarios" / f"{name}.yaml"
    if not path.is_file():
        raise WorkflowError(
            "unknown_scenario", "execution scenario is not on disk", {"scenario": name}
        )
    return path


def _scenario_document(name: str) -> dict[str, Any]:
    loaded = yaml.safe_load(_scenario_path(name).read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise WorkflowError(
            "scenario_invalid", "execution scenario is not a mapping", {"scenario": name}
        )
    return loaded


def _yaml_bases() -> list[dict[str, Any]]:
    folder = REPO / "config" / "runs"
    rows = []
    if not folder.is_dir():
        return rows
    for path in sorted(folder.glob("*.yaml")):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        strategy = document.get("strategy") if isinstance(document, dict) else None
        if not isinstance(strategy, dict) or strategy.get("kind") != "settlement_value":
            continue
        parameters = (
            strategy.get("parameters") if isinstance(strategy.get("parameters"), dict) else {}
        )
        rows.append(
            {
                "id": path.stem,
                "group": "source",
                "label": path.name,
                "source": str(path.relative_to(REPO)).replace("\\", "/"),
                "parameters": _plain(parameters),
            }
        )
    return rows


def strategy_capabilities(request: dict, root: Path) -> dict[str, Any]:
    del request
    defaults = settlement_value_config_as_parameters(
        settlement_value_config_from_parameters({}, file="capabilities"), full=True
    )
    fields = []
    for section, values in defaults.items():
        for name, value in values.items():
            key = f"{section}.{name}"
            kind, integer = _field_type(value)
            fields.append(
                {
                    "key": key,
                    "section": section,
                    "type": kind,
                    "integer": integer,
                    "default": _plain(value),
                    "choices": CHOICES.get(key),
                    "label": field_label(key),
                    "unit": field_unit(key, kind, integer),
                    "policy_owned": key in POLICY_FIELDS,
                }
            )
    scenarios = []
    for name in SCENARIO_NAMES:
        document = _scenario_document(name)
        scenarios.append(
            {
                "name": name,
                "label": SCENARIO_LABELS[name],
                "source": f"config/research/scenarios/{name}.yaml",
                "values": _flatten(document),
                "locked": ["schema_version", "adapter", "name"],
                "fields": [
                    {
                        "key": key,
                        "label": field_label(key),
                        "unit": field_unit(key, *_field_type(value)),
                        "type": _field_type(value)[0],
                        "integer": key in _SCENARIO_INTS or key.endswith(".exponent"),
                    }
                    for key, value in _flatten(document).items()
                ],
            }
        )
    return {
        "strategies": [
            {
                "kind": "settlement_value",
                "label": "Settlement value",
                "available": True,
                "reason": "",
            },
            {
                "kind": "ask70",
                "label": "ask70",
                "available": False,
                "reason": "workflow replay adapter is not available",
            },
            {
                "kind": "z_gap",
                "label": "z_gap",
                "available": False,
                "reason": "workflow replay adapter is not available",
            },
        ],
        "essentials": list(ESSENTIALS),
        "groups": list(GROUPS),
        "fields": fields,
        "scenarios": scenarios,
        "bases": [
            {
                "id": "defaults",
                "group": "source",
                "label": "Strategy schema defaults",
                "source": "SettlementValueConfig()",
                "parameters": defaults,
            }
        ]
        + _yaml_bases()
        + _saved_bases(root)
        + _candidate_bases(root),
        "policy": {
            key: {**value, "fields": POLICY_FIELDS} for key, value in REPLAY_POLICIES.items()
        },
        "default_scenario": DEFAULT_SCENARIO,
    }


def _policy_errors(parameters: dict[str, Any], research_policy: str | None) -> list[str]:
    if research_policy not in REPLAY_POLICIES:
        return ["This saved set has no named research policy. Replay will not invent one."]
    quality = parameters.get("quality")
    if (
        not isinstance(quality, dict)
        or "accepted_k_qualities" not in quality
        or "require_verified_settlement_spec" not in quality
    ):
        return [
            "Missing policy field quality.accepted_k_qualities or require_verified_settlement_spec."
        ]
    accepted = [str(item) for item in quality.get("accepted_k_qualities") or []]
    expected = list(POLICY_FIELDS["quality.accepted_k_qualities"])
    if (
        sorted(accepted) != sorted(expected)
        or quality.get("require_verified_settlement_spec") is not False
    ):
        return [
            "Submitted policy fields do not match this set. Save a different set to change them."
        ]
    return []


def _resolve_strategy(parameters: dict[str, Any]) -> dict[str, Any]:
    try:
        config = settlement_value_config_from_parameters(parameters, file="replay")
    except ConfigError as exc:
        raise WorkflowError("invalid_configuration", str(exc), {"field": exc.field}) from exc
    return settlement_value_config_as_parameters(config, full=True)


_SCENARIO_INTS = {
    "schema_version",
    "feed_delay_ms",
    "decision_delay_ms",
    "order_delay_ms",
    "cancel_delay_ms",
    "feedback_delay_ms",
    "adverse_price_ticks",
    "timer_interval_ms",
    "seed",
    "settlement_release_delay_ms",
    "final_gate_delay_ms",
    "gap_tolerance_ms",
    "fee_policy.exponent",
}


def _as_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or value is None or isinstance(value, str) and not value.strip():
        raise WorkflowError(
            "invalid_execution", f"{field} must be a whole number", {"field": field}
        )
    if isinstance(value, int):
        return value
    try:
        number = Decimal(str(value).strip())
    except Exception as exc:  # noqa: BLE001 - the value is user input
        raise WorkflowError(
            "invalid_execution", f"{field} must be a whole number", {"field": field}
        ) from exc
    if number != number.to_integral_value():
        raise WorkflowError(
            "invalid_execution", f"{field} must be a whole number", {"field": field}
        )
    return int(number)


def _as_text(value: Any, field: str) -> str:
    if isinstance(value, bool) or value is None:
        raise WorkflowError("invalid_execution", f"{field} is not text", {"field": field})
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(Decimal(str(value)), "f")
    return str(value)


def _coerce_scenario(value: Any, path: str = "") -> Any:
    if isinstance(value, dict):
        return {
            key: _coerce_scenario(item, f"{path}.{key}" if path else key)
            for key, item in value.items()
        }
    if path in _SCENARIO_INTS or path.endswith(".exponent"):
        return _as_int(value, path)
    return _as_text(value, path or "execution")


def _execution_field(message: str, document: dict[str, Any]) -> str | None:
    text = message.lower()
    if "timer_interval" in text:
        return "timer_interval_ms"
    if "fee" in text:
        return "fee_policy.rate"
    if "final_gate" in text:
        return "final_gate_delay_ms"
    if "participation" in text:
        return "participation"
    if "negative delay" in text:
        for key in (
            "feed_delay_ms",
            "decision_delay_ms",
            "order_delay_ms",
            "cancel_delay_ms",
            "feedback_delay_ms",
            "settlement_release_delay_ms",
            "final_gate_delay_ms",
            "gap_tolerance_ms",
        ):
            if isinstance(document.get(key), int) and document[key] < 0:
                return key
        return "order_delay_ms"
    token = message.split(" ", 1)[0]
    return token if token in document or token.startswith("fee_policy") else None


def _execution(
    name: str, overrides: dict[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    document = _scenario_document(name)
    flat = _flatten(document)
    for key, value in dict(overrides or {}).items():
        if key not in flat:
            raise WorkflowError(
                "unknown_execution_field", "execution field is not in the scenario", {"field": key}
            )
        if key in {"schema_version", "adapter", "name"} and value != flat[key]:
            raise WorkflowError(
                "execution_metadata",
                "scenario identity fields stay with the template",
                {"field": key},
            )
        flat[key] = value
    nested = _coerce_scenario(_unflatten(flat))
    try:
        scenario = strict(Scenario, nested)
        for key in (
            "feed_delay_ms",
            "decision_delay_ms",
            "order_delay_ms",
            "cancel_delay_ms",
            "feedback_delay_ms",
            "settlement_release_delay_ms",
        ):
            if getattr(scenario, key) < 0:
                raise WorkflowError("invalid_execution", "negative delay", {"field": key})
        scenario.validate()
    except WorkflowError:
        raise
    except (TypeError, ValueError) as exc:
        message = str(exc)
        raise WorkflowError(
            "invalid_execution", message, {"field": _execution_field(message, nested)}
        ) from exc
    typed = _flatten(asdict(scenario))
    for key in flat:
        if key in typed:
            flat[key] = typed[key]
    return _unflatten(flat), flat


def replay_validate(request: dict, root: Path) -> dict[str, Any]:
    del root
    if request.get("strategy_kind") not in (None, "settlement_value"):
        raise WorkflowError(
            "unsupported_strategy",
            "the workflow replay adapter is settlement_value",
            {"strategy_kind": request.get("strategy_kind")},
        )
    parameters = request.get("parameters")
    if not isinstance(parameters, dict):
        raise WorkflowError("missing_field", "parameters are required", {"field": "parameters"})
    policy_errors = _policy_errors(parameters, request.get("research_policy"))
    if policy_errors:
        return {"valid": False, "errors": policy_errors}
    try:
        resolved = _resolve_strategy(parameters)
        _document, execution = _execution(
            str(request.get("scenario") or DEFAULT_SCENARIO),
            request.get("execution") if isinstance(request.get("execution"), dict) else {},
        )
    except WorkflowError as exc:
        return {"valid": False, "errors": [exc.reason], "field": (exc.fields or {}).get("field")}
    policy_after = _policy_errors(resolved, request.get("research_policy"))
    if policy_after:
        return {"valid": False, "errors": policy_after}
    return {
        "valid": True,
        "errors": [],
        "strategy_kind": "settlement_value",
        "parameters": resolved,
        "scenario": request.get("scenario") or DEFAULT_SCENARIO,
        "execution": execution,
        "fingerprint": plan_digest({"parameters": resolved, "execution": execution}),
    }


def _saved_selection(root: Path, selection_id: str) -> dict[str, Any]:
    from .recipes import saved_selection, verify_saved_catalog

    saved = saved_selection(root, selection_id)
    if saved.get("recipe") == "missing_inputs":
        raise WorkflowError(
            "diagnostic_not_replayable",
            "a missing-input diagnostic set cannot open an ordinary replay",
            {"selection_id": selection_id},
        )
    verify_saved_catalog(saved)
    return saved


def replay_preview(request: dict, root: Path) -> dict[str, Any]:
    if request.get("units") or request.get("pin_parts"):
        raise WorkflowError(
            "units_refused", "the browser does not assemble replay units or pins", {}
        )
    if request.get("mode") not in (None, "replay"):
        raise WorkflowError(
            "mode_refused", "this operation previews Replay only", {"mode": request.get("mode")}
        )
    selection_id = request.get("selection_id")
    if not selection_id:
        raise WorkflowError("missing_field", "selection_id is required", {"field": "selection_id"})
    configurations = request.get("configurations")
    if not isinstance(configurations, list) or not configurations:
        raise WorkflowError(
            "missing_field", "at least one configuration is required", {"field": "configurations"}
        )
    saved = _saved_selection(root, str(selection_id))
    windows = [
        {
            "market_id": row["market_id"],
            "collection": row["collection"],
            "warehouse": row["warehouse"],
            "block_id": row.get("block_id"),
        }
        for row in saved.get("included") or []
    ]
    if not windows:
        raise WorkflowError(
            "empty_set", "the saved set has no windows", {"selection_id": selection_id}
        )
    policy = saved.get("research_policy")
    resolved_configs = []
    errors = []
    for index, raw in enumerate(configurations):
        if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
            errors.append(f"Configuration {index + 1} needs a name.")
            continue
        outcome = replay_validate(
            {
                "strategy_kind": raw.get("strategy_kind") or "settlement_value",
                "parameters": raw.get("parameters") or {},
                "scenario": raw.get("scenario") or DEFAULT_SCENARIO,
                "execution": raw.get("execution") or {},
                "research_policy": policy,
            },
            root,
        )
        if not outcome["valid"]:
            errors.extend(f"{raw.get('name')}: {item}" for item in outcome["errors"])
            continue
        document, flat = _execution(
            str(raw.get("scenario") or DEFAULT_SCENARIO),
            raw.get("execution") if isinstance(raw.get("execution"), dict) else {},
        )
        resolved_configs.append(
            {
                "name": str(raw["name"]).strip(),
                "strategy_kind": "settlement_value",
                "parameters": outcome["parameters"],
                "scenario": raw.get("scenario") or DEFAULT_SCENARIO,
                "execution": flat,
                "execution_document": document,
                "fingerprint": plan_digest(
                    {"parameters": outcome["parameters"], "execution": flat}
                ),
            }
        )
    if errors:
        return {"valid": False, "errors": errors, "selection_id": selection_id}
    groups: dict[str, list[dict]] = {}
    for config in resolved_configs:
        key = plan_digest(
            {"strategy_kind": config["strategy_kind"], "execution": config["execution"]}
        )
        groups.setdefault(key, []).append(config)
    batch_id = uuid.uuid4().hex
    batch_dir = root / "replay_batches" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    experiments = []
    for group in groups.values():
        experiment_id = uuid.uuid4().hex
        scenario_name = str(group[0]["scenario"])
        scenario_file = batch_dir / f"{experiment_id}-{scenario_name}.yaml"
        scenario_file.write_text(
            yaml.safe_dump(group[0]["execution_document"], sort_keys=False), encoding="utf-8"
        )
        units = []
        for window in windows:
            for index, config in enumerate(group):
                units.append(
                    {
                        "id": f"{window['market_id']}-{index}-{scenario_name}",
                        "kind": "strategy_replay",
                        "market_id": window["market_id"],
                        "warehouse": window["warehouse"],
                        "collection": window["collection"],
                        "block_id": window.get("block_id"),
                        "scenario_path": str(scenario_file),
                        "scenario_name": scenario_name,
                        "parameters": config["parameters"],
                        "configuration_name": config["name"],
                        "configuration_fingerprint": config["fingerprint"],
                    }
                )
        body = {
            "schema": "workflow_experiment_v1",
            "mode": "replay",
            "experiment_id": experiment_id,
            "batch_id": batch_id,
            "created_at": now_iso(),
            "selection_id": selection_id,
            "selection_digest": saved.get("digest"),
            "research_policy": policy,
            "strategy_kind": "settlement_value",
            "scenario_name": scenario_name,
            "configurations": [
                {
                    "name": item["name"],
                    "fingerprint": item["fingerprint"],
                    "parameters": item["parameters"],
                }
                for item in group
            ],
            "execution": group[0]["execution"],
            "units": units,
            "pins": {
                "manifests": saved.get("manifests") or [],
                "preview_id": saved.get("preview_id"),
            },
            "window_count": len(windows),
            "holdout": None,
            "objective": None,
            "limitation": saved.get("research_limitation"),
        }
        body["digest"] = plan_digest(
            {
                "selection_id": selection_id,
                "selection_digest": saved.get("digest"),
                # Execution belongs to the experiment's identity: two experiments that
                # differ only by an execution assumption are different experiments.
                "execution": group[0]["execution"],
                "units": [
                    {k: unit[k] for k in ("id", "market_id", "parameters", "scenario_name")}
                    for unit in units
                ],
            }
        )
        write_json(root / "experiments" / f"{experiment_id}.json", body)
        experiments.append(
            {
                "experiment_id": experiment_id,
                "digest": body["digest"],
                "strategy_kind": "settlement_value",
                "scenario_name": scenario_name,
                "configurations": [item["name"] for item in group],
                "resolved": [
                    {
                        "name": item["name"],
                        "fingerprint": item["fingerprint"],
                        "parameters": item["parameters"],
                    }
                    for item in group
                ],
                "execution": group[0]["execution"],
                "windows": len(windows),
                "units": len(units),
                "execution_fingerprint": plan_digest(group[0]["execution"]),
            }
        )
    batch = {
        "schema": "replay_batch_v1",
        "batch_id": batch_id,
        "name": str(request.get("name") or "Replay"),
        "created_at": now_iso(),
        "mode": "replay",
        "selection_id": selection_id,
        "selection_digest": saved.get("digest"),
        "research_policy": policy,
        "window_count": len(windows),
        "experiments": [item["experiment_id"] for item in experiments],
    }
    write_json(root / "replay_batches" / f"{batch_id}.json", batch)
    return {
        "valid": True,
        "errors": [],
        "batch_id": batch_id,
        "name": batch["name"],
        "selection_id": selection_id,
        "selection_digest": saved.get("digest"),
        "research_policy": policy,
        "windows": len(windows),
        "configurations": len(resolved_configs),
        "experiments": experiments,
        "units": sum(item["units"] for item in experiments),
        "holdout": None,
        "objective": None,
        "limitation": saved.get("research_limitation"),
        "reserved_overlap": _reserved(root, saved),
        **_sample_estimate(root, experiments),
    }


def _reserved(root: Path, saved: dict) -> list[dict]:
    from .sets import reserved_overlap

    return reserved_overlap(root, {row["market_id"] for row in saved.get("included") or []})


def _sample_estimate(root: Path, experiments: list[dict]) -> dict[str, Any]:
    """Expected entries and fills from earlier finished runs under the same scenario.

    A rough planning number: different parameters trade at different rates. It is shown so a
    run that cannot produce a comparable sample is noticed before it starts.
    """
    from .results import SMALL_FILLS, TOO_SMALL_FILLS, results_open

    jobs = _job_index(root)
    rates: dict[str, list[tuple[int, int, int]]] = {}
    for experiment_id, job in jobs.items():
        if job.get("state") != "completed" or not job.get("job_id"):
            continue
        path = root / "experiments" / f"{experiment_id}.json"
        if not path.is_file():
            continue
        scenario = str(read(path).get("scenario_name") or "")
        try:
            summary = results_open(
                {"job_id": job["job_id"], "all_configurations": True, "limit": 1}, root
            )["summary"]
        except Exception:  # noqa: BLE001 - an unreadable earlier run is skipped
            continue
        rates.setdefault(scenario, []).append(
            (summary["windows"], summary["attempts"], summary["fills"])
        )
    rows = []
    for experiment in experiments:
        sample = rates.get(experiment["scenario_name"]) or []
        windows = sum(item[0] for item in sample)
        if windows < 20:
            rows.append(
                {
                    "experiment_id": experiment["experiment_id"],
                    "basis_windows": windows,
                    "entries": None,
                    "fills": None,
                }
            )
            continue
        entry_rate = sum(item[1] for item in sample) / windows
        fill_rate = sum(item[2] for item in sample) / max(1, sum(item[1] for item in sample))
        entries = experiment["units"] * entry_rate
        fills = entries * fill_rate
        verdict = (
            "too_small"
            if fills < TOO_SMALL_FILLS
            else "small"
            if fills < SMALL_FILLS
            else "comparable"
        )
        rows.append(
            {
                "experiment_id": experiment["experiment_id"],
                "basis_windows": windows,
                "entries": round(entries, 1),
                "fills": round(fills, 1),
                "verdict": verdict,
            }
        )
    return {
        "estimate": rows,
        "estimate_reason": "earlier finished runs under the same scenario; parameters change rates",
    }


def _job_index(root: Path) -> dict[str, dict[str, Any]]:
    """Latest job per experiment, read once per request."""
    folder = root / "jobs"
    found: dict[str, dict[str, Any]] = {}
    if not folder.is_dir():
        return found
    for path in folder.glob("*/job.json"):
        body = read(path)
        experiment_id = str(body.get("experiment_id") or "")
        status_path = path.parent / "status.json"
        status = read(status_path) if status_path.is_file() else {}
        error = status.get("error")
        row = {
            "job_id": body.get("job_id"),
            "created_at": body.get("created_at") or "",
            "state": status.get("state"),
            "completed": status.get("completed"),
            "expected": status.get("expected"),
            "error": error.get("reason") if isinstance(error, dict) else error,
        }
        previous = found.get(experiment_id) or {}
        if str(row["created_at"]) >= str(previous.get("created_at") or ""):
            found[experiment_id] = row
    return found


def replay_history(request: dict, root: Path) -> dict[str, Any]:
    """Started replay jobs, newest first. A preview without a job is not a run."""
    from .artifacts import slice_page
    from .trash import run_status

    jobs = _job_index(root)
    rows = []
    strategy_rows: dict[str, dict[str, Any]] = {}
    selection_names: dict[str, str] = {}
    wanted = str(request.get("status") or "all")
    query = str(request.get("query") or "").strip().lower()
    folder = root / "replay_batches"
    previews = 0
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            batch = read(path)
            if batch.get("schema") != "replay_batch_v1":
                continue
            selection_id = str(batch.get("selection_id") or "")
            if selection_id not in selection_names:
                selection_path = root / "selections" / f"{selection_id}.json"
                saved = read(selection_path) if selection_path.is_file() else {}
                selection_names[selection_id] = str(saved.get("name") or "")
            for experiment_id in batch.get("experiments") or []:
                experiment_path = root / "experiments" / f"{experiment_id}.json"
                if not experiment_path.is_file():
                    continue
                experiment = read(experiment_path)
                job = jobs.get(str(experiment_id))
                kind = str(experiment.get("strategy_kind") or "settlement_value")
                if job is None:
                    previews += 1
                    if wanted != "previewed":
                        continue
                state = (job or {}).get("state") or "previewed"
                created = str((job or {}).get("created_at") or batch.get("created_at") or "")
                card = strategy_rows.setdefault(
                    kind,
                    {
                        "kind": kind,
                        "runs": 0,
                        "latest_name": None,
                        "latest_job_id": None,
                        "latest_state": None,
                        "latest_created_at": "",
                    },
                )
                if job:
                    card["runs"] = int(card["runs"]) + 1
                    if created >= str(card.get("latest_created_at") or ""):
                        card.update(
                            latest_job_id=job.get("job_id"),
                            latest_state=state,
                            latest_name=batch.get("name"),
                            latest_created_at=created,
                        )
                configurations = [
                    item.get("name") for item in experiment.get("configurations") or []
                ]
                text = " ".join(
                    [
                        str(batch.get("name") or ""),
                        str(experiment_id),
                        " ".join(str(item or "") for item in configurations),
                        selection_id,
                        selection_names.get(selection_id, ""),
                        str((job or {}).get("job_id") or ""),
                    ]
                ).lower()
                if query and query not in text:
                    continue
                if wanted != "all" and state != wanted:
                    continue
                rows.append(
                    {
                        "batch_id": batch.get("batch_id"),
                        "name": batch.get("name"),
                        "experiment_id": experiment_id,
                        "experiments_in_batch": len(batch.get("experiments") or []),
                        "job_id": (job or {}).get("job_id"),
                        "state": state,
                        "created_at": created,
                        "selection_id": selection_id,
                        "selection_name": selection_names.get(selection_id, ""),
                        "window_count": experiment.get("window_count"),
                        "strategy_kind": kind,
                        "scenario_name": experiment.get("scenario_name"),
                        "configurations": configurations,
                        "completed": (job or {}).get("completed") or 0,
                        "expected": (job or {}).get("expected")
                        or len(experiment.get("units") or []),
                        "error": (job or {}).get("error"),
                        "research_policy": experiment.get("research_policy"),
                        "status": run_status(
                            experiment,
                            (
                                root
                                / "selections"
                                / f"{experiment.get('selection_id') or selection_id}.json"
                            ).is_file(),
                        ),
                    }
                )
    reverse = str(request.get("order") or "newest") != "oldest"
    rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=reverse)
    page, cursor = slice_page(rows, request)
    return {
        "runs": page,
        "next_cursor": cursor,
        "cursor": int(request.get("cursor") or 0),
        "total": len(rows),
        "previews_without_job": previews,
        "strategies": list(strategy_rows.values()),
    }


def _job_for_experiment(root: Path, experiment_id: str) -> dict[str, Any] | None:
    return _job_index(root).get(experiment_id)


def _saved_bases(root: Path | None) -> list[dict[str, Any]]:
    folder = (root / "configurations") if root else None
    if folder is None or not folder.is_dir():
        return []
    rows = []
    for path in sorted(folder.glob("*.json")):
        body = read(path)
        if body.get("schema") != "replay_configuration_v1":
            continue
        rows.append(
            {
                "id": "saved:" + str(body["configuration_id"]),
                "group": "saved",
                "label": str(body.get("name") or body["configuration_id"]),
                "source": f"Saved configuration · {body.get('fingerprint')}",
                "parameters": body.get("parameters") or {},
                "scenario": body.get("scenario"),
                "execution": body.get("execution") or {},
                "fingerprint": body.get("fingerprint"),
                "created_at": body.get("created_at"),
            }
        )
    rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return rows


def _candidate_bases(root: Path | None) -> list[dict[str, Any]]:
    folder = (root / "candidates") if root else None
    if folder is None or not folder.is_dir():
        return []
    rows = []
    for path in sorted(folder.glob("*.freeze.json")):
        body = read(path)
        if body.get("strategy_kind") != "settlement_value" or not body.get("parameters"):
            continue
        rows.append(
            {
                "id": "candidate:" + str(body.get("candidate_id")),
                "group": "candidate",
                "label": str(body.get("name") or body.get("candidate_id")),
                "source": f"Frozen candidate · {body.get('digest')}",
                "parameters": body.get("parameters") or {},
                "scenario": body.get("scenario"),
                "execution": body.get("execution") or {},
            }
        )
    return rows


def configuration_save(request: dict, root: Path) -> dict[str, Any]:
    """Save a validated configuration for reuse. It is not added to any replay."""
    name = str(request.get("name") or "").strip()
    if not name:
        raise WorkflowError("missing_field", "a configuration needs a name", {"field": "name"})
    outcome = replay_validate(request, root)
    if not outcome["valid"]:
        raise WorkflowError(
            "invalid_configuration",
            "; ".join(outcome["errors"]),
            {"field": outcome.get("field")},
        )
    configuration_id = uuid.uuid4().hex
    body = {
        "schema": "replay_configuration_v1",
        "configuration_id": configuration_id,
        "name": name,
        "created_at": now_iso(),
        "strategy_kind": "settlement_value",
        "parameters": outcome["parameters"],
        "scenario": outcome["scenario"],
        "execution": outcome["execution"],
        "fingerprint": outcome["fingerprint"],
        "research_policy": request.get("research_policy"),
    }
    write_json(root / "configurations" / f"{configuration_id}.json", body)
    return {
        "configuration_id": configuration_id,
        "base_id": "saved:" + configuration_id,
        "fingerprint": body["fingerprint"],
        "name": name,
    }
