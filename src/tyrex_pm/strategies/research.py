"""How a strategy is offered in the research workspace (forms, defaults, identity).

A plugin that declares ``StrategyPlugin.research`` can be replayed from the workspace:
its parameters are edited in generated forms, validated by the plugin's own
``load_config``, and resolved back to a full parameter tree for display and identity.
Nothing in the workspace or the replay worker names a strategy.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from decimal import Decimal
from enum import Enum
from typing import Any


def plain(value: Any) -> Any:
    """JSON-friendly copy: Decimals as strings, enums as values, tuples as lists."""
    if is_dataclass(value) and not isinstance(value, type):
        return plain(asdict(value))
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, frozenset, set)):
        items = [plain(v) for v in value]
        return sorted(items, key=str) if isinstance(value, (frozenset, set)) else items
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return value.value
    return value


def config_parameters(config: Any) -> dict[str, Any]:
    """Default: a dataclass config as its parameter tree (the shape ``load_config`` reads)."""
    out = plain(config)
    if not isinstance(out, dict):
        raise TypeError("strategy config is not a dataclass")
    return out


@dataclass(frozen=True)
class ResearchSchema:
    # Valid starting parameters (``load_config(default_parameters)`` must succeed).
    default_parameters: Mapping[str, Any] = field(default_factory=dict)
    # Full parameter tree of a loaded config: shown in forms and stored with experiments.
    as_parameters: Callable[[Any], dict[str, Any]] = config_parameters
    # Parameter tree that identifies a configuration in result digests.
    identity_parameters: Callable[[Any], Any] = config_parameters
    label: str = ""
    # Form layout: groups of top-level sections, with optional labels and choices per key.
    groups: tuple[Mapping[str, Any], ...] = ()
    labels: Mapping[str, str] = field(default_factory=dict)
    choices: Mapping[str, list[str]] = field(default_factory=dict)
    essentials: tuple[str, ...] = ()
    # Fields the saved set's research policy fixes; the form shows them read-only.
    policy_fields: Mapping[str, Any] = field(default_factory=dict)
    # Protection block used when the plugin requires one and the configuration has none.
    default_protection: Mapping[str, Any] | None = None
