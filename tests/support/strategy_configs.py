"""Valid parameters for every registered strategy kind (taken from the shipped run configs).

Tests that must cover *every* strategy build their configs here, so adding a strategy means
adding one entry, never editing each test.
"""

from __future__ import annotations

from typing import Any

PARAMETERS: dict[str, dict[str, Any]] = {
    "ask70": {
        "entry_ask_threshold": "0.70",
        "leg_preference": "first_hit",
        "max_price_pad": "0.08",
        "tau_min_s": 105.0,
        "tau_max_s": 290.0,
        "max_clock_uncertainty_ms": 750.0,
    },
    "feature_probe": {
        "emit_entry": False,
        "tau_min_s": 105.0,
        "tau_max_s": 290.0,
        "max_clock_uncertainty_ms": 750.0,
    },
    "settlement_value": {
        "quality": {"accepted_k_qualities": ["CONFIRMED_CANONICAL", "PROVISIONAL"]},
    },
    "z_gap": {},
}

# The protection block of config/runs/ask70_protection_tiny_live.yaml.
PROTECTION: dict[str, Any] = {
    "mark": "bid",
    "size_mode": "full",
    "take_profit": {"style": "resting_gtd", "absolute": "0.85"},
    "stop_loss": {"style": "stop_market_fak", "absolute": "0.55"},
    "trailing": {"enabled": False, "activation": "immediate", "mark": "bid"},
}


def strategy_config(kind: str) -> Any:
    from tyrex_pm.strategies.registry import get_strategy_plugin

    return get_strategy_plugin(kind).load_config(PARAMETERS[kind], f"tests:{kind}")


def protection_for(kind: str, config: Any) -> Any:
    """The protection spec a run of this kind needs (None when the plugin needs none)."""
    from tyrex_pm.protection import protection_spec_from_mapping
    from tyrex_pm.strategies.registry import get_strategy_plugin, plugin_requires_protection

    if not plugin_requires_protection(get_strategy_plugin(kind), config):
        return None
    return protection_spec_from_mapping(PROTECTION, where="protection")
