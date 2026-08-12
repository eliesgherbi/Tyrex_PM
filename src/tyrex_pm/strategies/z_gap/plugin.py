"""z_gap strategy plugin — books + spot trades + chainlink/PTB."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tyrex_pm.facts.mappings import Z_GAP_INPUT_CONTRACT
from tyrex_pm.strategies.registry import StrategyPlugin
from tyrex_pm.strategies.z_gap.driver import create_z_gap_driver
from tyrex_pm.strategies.z_gap.schema import zgap_config_from_parameters


def _load_config(parameters: Mapping[str, Any], file: str) -> Any:
    return zgap_config_from_parameters(parameters, file=file)


def _tau_bounds(config: Any) -> tuple[float, float]:
    return config.entry.tau_min_s, config.entry.tau_max_s


def _clock_uncertainty_ms(config: Any) -> float:
    return float(config.ptb_time_quality.max_clock_uncertainty_ms)


def _validate_run_config(config: Any) -> None:
    if config.time_resolution.resolution_capability_default:
        raise ValueError(
            "automatic hold-to-resolution is not implemented by the execution lifecycle"
        )


ZGAP_PLUGIN = StrategyPlugin(
    kind="z_gap",
    input_contract=Z_GAP_INPUT_CONTRACT,
    load_config=_load_config,
    create_driver=create_z_gap_driver,
    entry_tau_bounds=_tau_bounds,
    max_clock_uncertainty_ms=_clock_uncertainty_ms,
    requires_protection=False,
    validate_run_config=_validate_run_config,
)
