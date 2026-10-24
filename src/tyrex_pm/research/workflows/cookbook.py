"""Set cookbook: named, versioned window-set recipes kept as files.

A recipe fixes the filters of a window set (time of day, weekdays, market-condition
ranges, percentile thresholds) so a researcher picks a card instead of re-entering them.
Recipes live in ``config/research/set_recipes/*.yaml``. A set made from a recipe stores the
recipe id, version, the parameters used and a copy of the recipe, so it can be re-derived
after the file changes. Percentile thresholds are resolved against the windows in scope at
preview time (see ``recipes.resolve_recipe``) and the resolved values are saved with the set.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import yaml

from .artifacts import REPO, plan_digest
from .contracts import WorkflowError
from .features import FEATURE_KEYS

COOKBOOK = REPO / "config" / "research" / "set_recipes"
SCHEMA = "set_recipe_v1"
PARAMETER_TYPES = ("time", "weekdays", "quantile", "number")
_TIME = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


def _folder() -> Path:
    return COOKBOOK


def _validate(body: dict, source: str) -> dict[str, Any]:
    from .recipes import RESEARCH_POLICIES, _validate_filters

    def refuse(reason: str) -> WorkflowError:
        return WorkflowError("invalid_recipe", reason, {"recipe": source})

    if not isinstance(body, dict) or body.get("schema") != SCHEMA:
        raise refuse(f"a recipe declares schema: {SCHEMA}")
    for key in ("id", "version", "title", "summary", "policy"):
        if body.get(key) in (None, ""):
            raise refuse(f"a recipe needs {key}")
    if not re.fullmatch(r"[a-z0-9_]+", str(body["id"])):
        raise refuse("a recipe id uses lowercase letters, digits and _")
    if str(body["policy"]) not in RESEARCH_POLICIES:
        raise refuse("a recipe policy is a research coverage: " + ", ".join(RESEARCH_POLICIES))
    quantiles = body.get("quantiles") or {}
    for key, bounds in quantiles.items():
        if key not in FEATURE_KEYS or not isinstance(bounds, list) or len(bounds) != 2:
            raise refuse(f"quantile {key} is [low, high] on a known window feature")
        for bound in bounds:
            if bound is not None and not 0 <= float(bound) <= 1:
                raise refuse("percentiles are between 0 and 1")
    parameters = body.get("parameters") or []
    for item in parameters:
        if item.get("type") not in PARAMETER_TYPES or not item.get("path"):
            raise refuse("a parameter has a path and a type: " + ", ".join(PARAMETER_TYPES))
        _get(body, str(item["path"]))  # the default must exist
    filters = _validate_filters(body.get("filters") or {})
    return {
        "schema": SCHEMA,
        "id": str(body["id"]),
        "version": int(body["version"]),
        "title": str(body["title"]),
        "summary": " ".join(str(body["summary"]).split()),
        "why": " ".join(str(body.get("why") or "").split()),
        "policy": str(body["policy"]),
        "filters": filters,
        "quantiles": {key: list(bounds) for key, bounds in quantiles.items()},
        "parameters": [
            {"path": str(item["path"]), "label": str(item.get("label") or item["path"]),
             "type": str(item["type"])}
            for item in parameters
        ],
    }


def _walk(body: dict, path: str) -> tuple[Any, str]:
    """Parent container and last key of a parameter path.

    Paths start with ``quantiles.`` for percentile bounds; anything else is a filter field,
    e.g. ``daily_time.start`` or ``weekdays``.
    """
    parts = path.split(".")
    if parts[0] == "quantiles":
        node: Any = body.get("quantiles") or {}
        parts = parts[1:]
    else:
        node = body.get("filters") or {}
    try:
        for part in parts[:-1]:
            node = node[int(part)] if isinstance(node, list) else node[part]
        last = parts[-1]
        node[int(last)] if isinstance(node, list) else node[last]
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise WorkflowError("invalid_recipe", "parameter path not found", {"path": path}) from exc
    return node, last


def _get(body: dict, path: str) -> Any:
    node, last = _walk(body, path)
    return node[int(last)] if isinstance(node, list) else node[last]


def _set(body: dict, path: str, value: Any) -> None:
    node, last = _walk(body, path)
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value


def _check_value(kind: str, value: Any, path: str) -> Any:
    def refuse() -> WorkflowError:
        return WorkflowError("invalid_parameter", f"{path} is not a valid {kind}", {"path": path})

    if kind == "time":
        if not isinstance(value, str) or not _TIME.match(value):
            raise refuse()
        return value
    if kind == "weekdays":
        try:
            days = sorted({int(day) for day in value})
        except (TypeError, ValueError) as exc:
            raise refuse() from exc
        if not days or any(day < 1 or day > 7 for day in days):
            raise refuse()
        return days
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise refuse() from exc
    if kind == "quantile" and not 0 <= number <= 1:
        raise refuse()
    return number


def load(recipe_id: str) -> dict[str, Any]:
    path = _folder() / f"{recipe_id}.yaml"
    if not re.fullmatch(r"[a-z0-9_]+", recipe_id) or not path.is_file():
        raise WorkflowError("unknown_recipe", "recipe is not in the cookbook", {"id": recipe_id})
    body = _validate(yaml.safe_load(path.read_text(encoding="utf-8")), path.name)
    if body["id"] != recipe_id:
        raise WorkflowError("invalid_recipe", "the file name and id differ", {"id": recipe_id})
    return body


def recipes() -> list[dict[str, Any]]:
    rows = []
    for path in sorted(_folder().glob("*.yaml")) if _folder().is_dir() else []:
        try:
            body = load(path.stem)
        except WorkflowError as exc:
            rows.append({"id": path.stem, "invalid": exc.reason})
            continue
        body["digest"] = plan_digest(body)
        body["defaults"] = {item["path"]: _get(body, item["path"]) for item in body["parameters"]}
        rows.append(body)
    return rows


def apply_recipe(spec: dict) -> dict[str, Any]:
    """Filters, quantiles and the record a set keeps, for one recipe and its parameters.

    ``spec`` is {id, version, parameters} from the builder, or the ``cookbook`` record of a
    saved set (which carries a snapshot of the recipe, so a later file change does not
    change how that set is re-derived).
    """
    snapshot = spec.get("snapshot")
    if snapshot:
        body = _validate(copy.deepcopy(snapshot), f"snapshot of {spec.get('id')}")
    else:
        body = load(str(spec.get("id") or ""))
        wanted = spec.get("version")
        if wanted is not None and int(wanted) != body["version"]:
            raise WorkflowError(
                "recipe_changed",
                "the recipe file changed since this card was opened; reopen it",
                {"id": body["id"], "version": body["version"], "requested": wanted},
            )
    original = copy.deepcopy(body)
    kinds = {item["path"]: item["type"] for item in body["parameters"]}
    parameters = {}
    for path, value in (spec.get("parameters") or {}).items():
        if path not in kinds:
            raise WorkflowError(
                "invalid_parameter", "this recipe does not expose that parameter", {"path": path}
            )
        parameters[path] = _check_value(kinds[path], value, path)
        _set(body, path, parameters[path])
    from .recipes import _validate_filters

    filters = _validate_filters(body["filters"])
    return {
        "filters": filters,
        "quantiles": body["quantiles"],
        "policy": body["policy"],
        "record": {
            "id": body["id"],
            "version": body["version"],
            "title": body["title"],
            "digest": plan_digest(original),
            "parameters": parameters,
            "snapshot": original,
        },
    }


def resolve_quantiles(quantiles: dict, features: dict[str, dict]) -> tuple[dict, dict]:
    """Absolute condition ranges for percentile bounds, from the given windows' features."""
    conditions, resolved = {}, {}
    for key, (low_q, high_q) in quantiles.items():
        values = sorted(item[key] for item in features.values() if item.get(key) is not None)
        if not values:
            raise WorkflowError(
                "quantile_population_empty",
                f"no window in scope has a {key} value to rank",
                {"feature": key},
            )

        def pick(q: float | None) -> float | None:
            return None if q is None else values[min(len(values) - 1, int(q * (len(values) - 1)))]

        bounds = [pick(low_q), pick(high_q)]
        conditions[key] = bounds
        resolved[key] = {"percentiles": [low_q, high_q], "values": bounds, "ranked": len(values)}
    return conditions, resolved


def cookbook_list(request: dict, root: Path) -> dict[str, Any]:
    del request, root
    return {"recipes": recipes(), "folder": str(_folder().relative_to(REPO)).replace("\\", "/")}


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:48]


def cookbook_save(request: dict, root: Path) -> dict[str, Any]:
    """Save the filters of a custom set as a new cookbook recipe (never overwrites)."""
    del root
    title = str(request.get("title") or "").strip()
    if not title:
        raise WorkflowError("missing_field", "a recipe needs a title", {"field": "title"})
    recipe_id = _slug(title)
    if not recipe_id:
        raise WorkflowError("invalid_recipe", "the title needs letters or digits", {})
    path = _folder() / f"{recipe_id}.yaml"
    if path.exists():
        raise WorkflowError(
            "recipe_exists", "a recipe with this name exists; choose another title",
            {"id": recipe_id},
        )
    filters = dict(request.get("filters") or {})
    filters.pop("picked", None)
    filters["selection_mode"] = "all_matches"
    parameters = []
    if (filters.get("daily_time") or {}).get("enabled"):
        parameters += [
            {"path": "daily_time.start", "label": "From", "type": "time"},
            {"path": "daily_time.end", "label": "To", "type": "time"},
        ]
    if filters.get("weekdays"):
        parameters.append({"path": "weekdays", "label": "Weekdays", "type": "weekdays"})
    body = {
        "schema": SCHEMA,
        "id": recipe_id,
        "version": 1,
        "title": title,
        "summary": str(request.get("summary") or "").strip()
        or f"Saved from a custom set: {title}.",
        "why": str(request.get("why") or "").strip(),
        "policy": str(request.get("policy") or "research_v2_1"),
        "filters": filters,
        "quantiles": {},
        "parameters": parameters,
    }
    checked = _validate(body, path.name)
    if filters.get("outcome", "any") != "any" or filters.get("label_source", "any") != "any":
        raise WorkflowError(
            "invalid_recipe",
            "a recipe cannot select on the outcome; that would condition results on the answer",
            {},
        )
    _folder().mkdir(parents=True, exist_ok=True)
    header = (
        "# Set cookbook recipe saved from the Data Library. Edit the values, then raise\n"
        "# version so sets made from the older version stay distinguishable.\n"
    )
    path.write_text(
        header + yaml.safe_dump(body, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return {"id": checked["id"], "version": checked["version"], "path": str(path)}
