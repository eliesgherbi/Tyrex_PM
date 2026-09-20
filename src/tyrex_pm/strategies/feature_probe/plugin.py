"""feature_probe strategy plugin — books + spot L2 imbalance."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tyrex_pm.facts.mappings import FEATURE_PROBE_INPUT_CONTRACT
from tyrex_pm.strategies.feature_probe.driver import create_feature_probe_driver
from tyrex_pm.strategies.feature_probe.schema import feature_probe_config_from_parameters
from tyrex_pm.strategies.registry import StrategyPlugin


def _load_config(parameters: Mapping[str, Any], file: str) -> Any:
    return feature_probe_config_from_parameters(parameters, file=file)


def _tau_bounds(config: Any) -> tuple[float, float]:
    return config.tau_min_s, config.tau_max_s


def _clock_uncertainty_ms(config: Any) -> float:
    return float(config.max_clock_uncertainty_ms)


def _requires_protection(config: Any) -> bool:
    return bool(config.emit_entry)


FEATURE_PROBE_PLUGIN = StrategyPlugin(
    kind="feature_probe",
    input_contract=FEATURE_PROBE_INPUT_CONTRACT,
    load_config=_load_config,
    create_driver=create_feature_probe_driver,
    entry_tau_bounds=_tau_bounds,
    max_clock_uncertainty_ms=_clock_uncertainty_ms,
    requires_protection=_requires_protection,
)
