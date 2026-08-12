"""ask70 strategy plugin — book-only input contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tyrex_pm.facts.mappings import ASK70_INPUT_CONTRACT
from tyrex_pm.strategies.ask70.driver import create_ask70_driver
from tyrex_pm.strategies.ask70.schema import ask70_config_from_parameters
from tyrex_pm.strategies.registry import StrategyPlugin


def _load_config(parameters: Mapping[str, Any], file: str) -> Any:
    return ask70_config_from_parameters(parameters, file=file)


def _tau_bounds(config: Any) -> tuple[float, float]:
    return config.tau_min_s, config.tau_max_s


def _clock_uncertainty_ms(config: Any) -> float:
    return float(config.max_clock_uncertainty_ms)


ASK70_PLUGIN = StrategyPlugin(
    kind="ask70",
    input_contract=ASK70_INPUT_CONTRACT,
    load_config=_load_config,
    create_driver=create_ask70_driver,
    entry_tau_bounds=_tau_bounds,
    max_clock_uncertainty_ms=_clock_uncertainty_ms,
    requires_protection=True,
)
