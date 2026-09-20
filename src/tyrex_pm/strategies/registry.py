"""Open strategy plugin registry — adding a kind must not edit run_config switches."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from tyrex_pm.facts.contract import InputContract
from tyrex_pm.indicators.spec import IndicatorSpec

_REGISTRY: dict[str, StrategyPlugin] = {}


@dataclass(frozen=True)
class StrategyPlugin:
    kind: str
    input_contract: InputContract
    load_config: Callable[[Mapping[str, Any], str], Any]
    create_driver: Callable[..., Any]
    entry_tau_bounds: Callable[[Any], tuple[float, float]]
    max_clock_uncertainty_ms: Callable[[Any], float]
    requires_protection: bool | Callable[[Any], bool] = False
    validate_run_config: Callable[[Any], None] | None = None
    indicator_specs: Callable[[Any], tuple[IndicatorSpec, ...]] | None = None


def register_strategy(plugin: StrategyPlugin) -> None:
    if not plugin.kind.strip():
        raise ValueError("strategy plugin kind must be non-empty")
    existing = _REGISTRY.get(plugin.kind)
    if existing is not None and existing is not plugin:
        raise ValueError(f"strategy {plugin.kind!r} is already registered")
    _REGISTRY[plugin.kind] = plugin


def ensure_strategies_registered() -> None:
    if _REGISTRY:
        return
    from tyrex_pm.strategies.ask70.plugin import ASK70_PLUGIN
    from tyrex_pm.strategies.feature_probe.plugin import FEATURE_PROBE_PLUGIN
    from tyrex_pm.strategies.z_gap.plugin import ZGAP_PLUGIN

    register_strategy(ASK70_PLUGIN)
    register_strategy(FEATURE_PROBE_PLUGIN)
    register_strategy(ZGAP_PLUGIN)


def get_strategy_plugin(kind: str) -> StrategyPlugin:
    ensure_strategies_registered()
    plugin = _REGISTRY.get(kind)
    if plugin is None:
        available = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise KeyError(f"strategy {kind!r} is not registered; available: {available}")
    return plugin


def registered_strategy_kinds() -> tuple[str, ...]:
    ensure_strategies_registered()
    return tuple(sorted(_REGISTRY))


def plugin_indicator_specs(plugin: StrategyPlugin, config: Any) -> tuple[IndicatorSpec, ...]:
    """Parameterized specs from loaded config, else the contract's static tuple."""
    if plugin.indicator_specs is None:
        return plugin.input_contract.indicators
    return plugin.indicator_specs(config)


def plugin_requires_protection(plugin: StrategyPlugin, config: Any) -> bool:
    """Static flag or config-dependent callable (e.g. feature_probe emit_entry)."""
    flag = plugin.requires_protection
    if callable(flag):
        return bool(flag(config))
    return bool(flag)

