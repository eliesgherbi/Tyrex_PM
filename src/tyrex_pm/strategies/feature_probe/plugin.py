"""feature_probe strategy plugin — books + spot L2 imbalance."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tyrex_pm.facts.mappings import FEATURE_PROBE_INPUT_CONTRACT
from tyrex_pm.strategies.feature_probe.driver import create_feature_probe_driver
from tyrex_pm.strategies.feature_probe.schema import feature_probe_config_from_parameters
from tyrex_pm.strategies.registry import StrategyPlugin
from tyrex_pm.strategies.research import ResearchSchema


def _load_config(parameters: Mapping[str, Any], file: str) -> Any:
    return feature_probe_config_from_parameters(parameters, file=file)


def _tau_bounds(config: Any) -> tuple[float, float]:
    return config.tau_min_s, config.tau_max_s


def _clock_uncertainty_ms(config: Any) -> float:
    return float(config.max_clock_uncertainty_ms)


def _requires_protection(config: Any) -> bool:
    return bool(config.emit_entry)


# Starting point in the research workspace: config/runs/feature_probe_catalog.yaml.
FEATURE_PROBE_RESEARCH = ResearchSchema(
    label="feature_probe (input check)",
    default_parameters={"emit_entry": False, "tau_min_s": 105.0, "tau_max_s": 290.0,
                        "max_clock_uncertainty_ms": 750.0},
    essentials=("emit_entry", "tau_min_s", "tau_max_s"),
)


FEATURE_PROBE_PLUGIN = StrategyPlugin(
    kind="feature_probe",
    research=FEATURE_PROBE_RESEARCH,
    input_contract=FEATURE_PROBE_INPUT_CONTRACT,
    load_config=_load_config,
    create_driver=create_feature_probe_driver,
    entry_tau_bounds=_tau_bounds,
    max_clock_uncertainty_ms=_clock_uncertainty_ms,
    requires_protection=_requires_protection,
)
