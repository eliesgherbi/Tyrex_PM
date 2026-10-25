"""ask70 strategy plugin — book-only input contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tyrex_pm.facts.mappings import ASK70_INPUT_CONTRACT
from tyrex_pm.strategies.ask70.driver import create_ask70_driver
from tyrex_pm.strategies.ask70.schema import ask70_config_from_parameters
from tyrex_pm.strategies.registry import StrategyPlugin
from tyrex_pm.strategies.research import ResearchSchema


def _load_config(parameters: Mapping[str, Any], file: str) -> Any:
    return ask70_config_from_parameters(parameters, file=file)


def _tau_bounds(config: Any) -> tuple[float, float]:
    return config.tau_min_s, config.tau_max_s


def _clock_uncertainty_ms(config: Any) -> float:
    return float(config.max_clock_uncertainty_ms)


# Starting point in the research workspace: config/runs/ask70_protection_tiny_live.yaml.
ASK70_RESEARCH = ResearchSchema(
    label="ask70 (ask threshold)",
    default_parameters={
        "entry_ask_threshold": "0.70",
        "leg_preference": "first_hit",
        "max_price_pad": "0.08",
        "tau_min_s": 105.0,
        "tau_max_s": 290.0,
        "max_clock_uncertainty_ms": 750.0,
    },
    essentials=("entry_ask_threshold", "max_price_pad", "tau_min_s", "tau_max_s"),
    choices={"leg_preference": ["first_hit", "up_then_down", "down_then_up"],
             "liquidity_role": ["taker", "maker"]},
    default_protection={
        "mark": "bid",
        "size_mode": "full",
        "take_profit": {"style": "resting_gtd", "absolute": "0.85"},
        "stop_loss": {"style": "stop_market_fak", "absolute": "0.55"},
        "trailing": {"enabled": False, "activation": "immediate", "mark": "bid"},
    },
)


ASK70_PLUGIN = StrategyPlugin(
    kind="ask70",
    research=ASK70_RESEARCH,
    input_contract=ASK70_INPUT_CONTRACT,
    load_config=_load_config,
    create_driver=create_ask70_driver,
    entry_tau_bounds=_tau_bounds,
    max_clock_uncertainty_ms=_clock_uncertainty_ms,
    requires_protection=True,
)
