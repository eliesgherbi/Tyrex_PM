"""z_gap strategy plugin — books + spot trades + chainlink/PTB."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tyrex_pm.facts import ids as F
from tyrex_pm.facts.mappings import Z_GAP_INPUT_CONTRACT
from tyrex_pm.indicators.spec import IndicatorSpec
from tyrex_pm.strategies.registry import StrategyPlugin
from tyrex_pm.strategies.research import ResearchSchema
from tyrex_pm.strategies.z_gap.config import ZGapVolatilityConfig
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


def ewma_vol_spec_from_volatility(volatility: ZGapVolatilityConfig) -> IndicatorSpec:
    return IndicatorSpec(
        name="ewma_vol",
        source=F.BINANCE_SPOT_TRADES,
        gates_entry=True,
        params=(
            ("half_life_s", str(volatility.half_life_s)),
            ("min_samples_s", str(volatility.min_samples_s)),
            ("jump_threshold_sigma", str(volatility.jump_threshold_sigma)),
            ("sample_interval_s", str(volatility.sample_interval_s)),
            ("tau_floor_s", str(volatility.tau_floor_s)),
            ("sigma_floor", str(volatility.sigma_floor)),
        ),
    )


def _indicator_specs(config: Any) -> tuple[IndicatorSpec, ...]:
    return (ewma_vol_spec_from_volatility(config.volatility),)


ZGAP_RESEARCH = ResearchSchema(
    label="z_gap (fair-value gap)",
    essentials=(
        "entry.theta_take", "entry.z_min", "entry.z_max", "entry.tau_min_s", "entry.tau_max_s",
        "volatility.half_life_s",
    ),
    groups=(
        {"id": "entry", "name": "Entry", "description": "Edge, z-score band and timing.",
         "sections": ["entry"]},
        {"id": "model", "name": "Model & volatility", "description": "Volatility estimator.",
         "sections": ["volatility"]},
        {"id": "exit", "name": "Exit & thesis", "description": "Realization, stop and timing.",
         "sections": ["realization", "thesis", "time_resolution"]},
        {"id": "inputs", "name": "Inputs & friction",
         "description": "Price-to-beat quality, fees and resolution penalty.",
         "sections": ["ptb_time_quality", "friction"]},
    ),
)


ZGAP_PLUGIN = StrategyPlugin(
    kind="z_gap",
    research=ZGAP_RESEARCH,
    input_contract=Z_GAP_INPUT_CONTRACT,
    load_config=_load_config,
    create_driver=create_z_gap_driver,
    entry_tau_bounds=_tau_bounds,
    max_clock_uncertainty_ms=_clock_uncertainty_ms,
    requires_protection=False,
    validate_run_config=_validate_run_config,
    indicator_specs=_indicator_specs,
)
