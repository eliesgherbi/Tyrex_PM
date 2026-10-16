"""settlement_value strategy plugin — books + spot trades + settlement reference + sealed K."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tyrex_pm.facts import ids as F
from tyrex_pm.facts.contract import InputContract, OnFact, OnTimer
from tyrex_pm.indicators.spec import IndicatorSpec
from tyrex_pm.strategies.registry import StrategyPlugin
from tyrex_pm.strategies.settlement_value.config import (
    SettlementValueConfig,
    SettlementVolatilityConfig,
)
from tyrex_pm.strategies.settlement_value.driver import create_settlement_value_driver
from tyrex_pm.strategies.settlement_value.schema import settlement_value_config_from_parameters

SETTLEMENT_VALUE_INPUT_CONTRACT = InputContract(
    required=(
        F.POLYMARKET_BOOKS,
        F.BINANCE_SPOT_TRADES,
        F.CHAINLINK_TWAP,
        F.CLOCK_SYNC,
        F.ACCOUNT_SNAPSHOT,
        F.PTB_SEALED,
        F.REFERENCE_ALIGNED,
        F.POLYMARKET_MARKET_META,
    ),
    optional=(),
    indicators=(IndicatorSpec(name="ewma_vol", source=F.BINANCE_SPOT_TRADES),),
    evaluate_on=(
        OnFact(F.BINANCE_SPOT_TRADES, coalesce_s=1.0, after_ready=F.PTB_SEALED),
        OnFact(F.POLYMARKET_BOOKS, coalesce_s=1.0, after_ready=F.PTB_SEALED),
        OnTimer(interval_s=1.0),
    ),
)

HOLD_TO_RESOLUTION = "hold_to_resolution"


def _load_config(parameters: Mapping[str, Any], file: str) -> SettlementValueConfig:
    return settlement_value_config_from_parameters(parameters, file=file)


def _tau_bounds(config: SettlementValueConfig) -> tuple[float, float]:
    return config.entry.tau_min_s, config.entry.tau_max_s


def _clock_uncertainty_ms(config: SettlementValueConfig) -> float:
    return float(config.quality.max_clock_uncertainty_ms)


def _validate_run_config(config: SettlementValueConfig) -> None:
    """Live (supervised-trial) requirements; the host separately requires the
    ``hold_to_resolution`` lifecycle, a settlement block and verified constraints."""
    if config.entry.target_mode != "cap_only":
        raise ValueError(
            "settlement_value live runs require entry.target_mode: cap_only; sizing is "
            "budget-constrained and the $3 objective stays a research target"
        )


def _lifecycle_modes(config: SettlementValueConfig) -> frozenset[str]:
    # A forced pre-expiry sale must never run under this strategy's name.
    del config
    return frozenset({HOLD_TO_RESOLUTION})


def ewma_vol_spec(volatility: SettlementVolatilityConfig) -> IndicatorSpec:
    return IndicatorSpec(
        name="ewma_vol",
        source=F.BINANCE_SPOT_TRADES,
        params=(
            ("half_life_s", str(volatility.half_life_s)),
            ("min_samples_s", str(volatility.min_samples_s)),
            ("jump_threshold_sigma", str(volatility.jump_threshold_sigma)),
            ("sample_interval_s", str(volatility.sample_interval_s)),
            ("tau_floor_s", str(volatility.tau_floor_s)),
            ("sigma_floor", str(volatility.sigma_floor)),
        ),
    )


def spot_path_spec(config: SettlementValueConfig) -> IndicatorSpec:
    # Long enough for the basis window behind a lagged report plus the observed window.
    window = 3 * config.model.averaging_window_s
    return IndicatorSpec(
        name="spot_path",
        source=F.BINANCE_SPOT_TRADES,
        params=(("window_s", str(window)), ("sample_interval_s", "1")),
    )


def _indicator_specs(config: SettlementValueConfig) -> tuple[IndicatorSpec, ...]:
    return (ewma_vol_spec(config.volatility), spot_path_spec(config))


SETTLEMENT_VALUE_PLUGIN = StrategyPlugin(
    kind="settlement_value",
    input_contract=SETTLEMENT_VALUE_INPUT_CONTRACT,
    load_config=_load_config,
    create_driver=create_settlement_value_driver,
    entry_tau_bounds=_tau_bounds,
    max_clock_uncertainty_ms=_clock_uncertainty_ms,
    requires_protection=False,
    validate_run_config=_validate_run_config,
    indicator_specs=_indicator_specs,
    lifecycle_modes=_lifecycle_modes,
)
