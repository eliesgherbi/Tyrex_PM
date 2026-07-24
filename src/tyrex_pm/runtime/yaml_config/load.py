"""YAML safe loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

from tyrex_pm.runtime.yaml_config.errors import ConfigError


def load_yaml_mapping(path: Path | str) -> dict[str, Any]:
    """Load a YAML file that must decode to a mapping."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"configuration file not found: {p}", file=str(p))
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file: {exc}", file=str(p)) from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}", file=str(p)) from exc
    if raw is None:
        raise ConfigError("YAML document is empty", file=str(p))
    if not isinstance(raw, Mapping):
        raise ConfigError(
            f"YAML root must be a mapping, got {type(raw).__name__}",
            file=str(p),
        )
    return dict(raw)
