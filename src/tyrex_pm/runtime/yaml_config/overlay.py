"""Schema-aware leaf-level scenario overlay."""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping

from tyrex_pm.runtime.yaml_config.errors import ConfigError

_SCENARIO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_scenario_name(name: str) -> str:
    if not name or not _SCENARIO_NAME_RE.match(name):
        raise ConfigError(
            "invalid scenario name (use letters, digits, '.', '_', '-' only; "
            "no path separators)",
            field="scenario",
        )
    if ".." in name or "/" in name or "\\" in name:
        raise ConfigError("scenario name must not contain path segments", field="scenario")
    return name


def apply_leaf_overlay(
    base: dict[str, Any],
    overlay: Mapping[str, Any],
    *,
    allowed_paths: frozenset[str],
    file: str | None,
    path_prefix: str = "",
) -> dict[str, Any]:
    """Deep-merge overlay into base; only known leaf paths may be set.

    Intermediate mappings are allowed only to reach leaves. Setting a mapping
    where the schema expects a scalar leaf fails. Replacing a whole section
    object in one assignment is rejected when the section has nested leaves —
    callers must set individual leaves.
    """
    result = copy.deepcopy(base)
    _apply(result, overlay, allowed_paths=allowed_paths, file=file, path=path_prefix)
    return result


def _apply(
    target: dict[str, Any],
    overlay: Mapping[str, Any],
    *,
    allowed_paths: frozenset[str],
    file: str | None,
    path: str,
) -> None:
    for key, value in overlay.items():
        child_path = f"{path}.{key}" if path else key
        # Any path that equals an allowed leaf, or is a prefix of allowed leaves.
        leaf_match = child_path in allowed_paths
        prefix_match = any(
            p == child_path or p.startswith(child_path + ".") for p in allowed_paths
        )
        if not leaf_match and not prefix_match:
            raise ConfigError(
                f"unknown scenario path (not in schema): {child_path}",
                file=file,
                field=child_path,
            )
        if leaf_match:
            if isinstance(value, Mapping):
                raise ConfigError(
                    f"scenario path {child_path} is a leaf and cannot be a mapping",
                    file=file,
                    field=child_path,
                )
            target[key] = value
            continue
        # Intermediate mapping toward leaves.
        if not isinstance(value, Mapping):
            raise ConfigError(
                f"scenario path {child_path} has nested fields; override leaves "
                f"individually (refusing whole-section replacement)",
                file=file,
                field=child_path,
            )
        if key not in target or not isinstance(target.get(key), dict):
            target[key] = {}
        assert isinstance(target[key], dict)
        _apply(
            target[key],
            value,
            allowed_paths=allowed_paths,
            file=file,
            path=child_path,
        )
