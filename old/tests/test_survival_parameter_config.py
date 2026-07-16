"""Survival config parsing defaults and Phase 1 parameter wiring."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from scripts.preflight_phase1_live_scenario import validate_phase1_live_app
from tyrex_pm.runtime.config import SurvivalConfig, _parse_survival_config, load_app_config

REPO = Path(__file__).resolve().parents[1]


def test_survival_defaults_are_advisory() -> None:
    cfg = SurvivalConfig()
    assert cfg.enabled is False
    assert cfg.target_policy.mode == "dynamic"
    assert cfg.trailing_stop.enforcement_mode == "advisory"
    assert cfg.stall_exit.enforcement_mode == "advisory"
    assert cfg.reachability.enforcement_mode == "advisory"
    assert cfg.economics.enforcement_mode == "advisory"


def test_parse_survival_target_policy_overrides() -> None:
    cfg = _parse_survival_config(
        {
            "enabled": True,
            "target_policy": {
                "mode": "dynamic",
                "small_loss_max_usd_per_pair": "0.08",
                "small_profit_min_usd_per_pair": "0.04",
                "max_reasonable_exit_price": "0.95",
                "slippage_buffer": "0.01",
            },
            "trailing_stop": {"enforcement_mode": "enforce"},
        }
    )
    assert cfg.enabled is True
    assert cfg.target_policy.mode == "dynamic"
    assert cfg.target_policy.small_loss_max_usd_per_pair == Decimal("0.08")
    assert cfg.target_policy.max_reasonable_exit_price == Decimal("0.95")
    assert cfg.target_policy.slippage_buffer == Decimal("0.01")
    assert cfg.trailing_stop.enforcement_mode == "enforce"


def test_phase1_profiles_load_dynamic_target_policy() -> None:
    for scenario in (
        "live_paired_binary_phase1_advisory",
        "live_paired_binary_phase1_target_only",
        "live_paired_binary_phase1_trailing_enforce",
        "live_paired_binary_phase1_stall_enforce",
    ):
        app = load_app_config(
            repo_root=REPO,
            strategy_file="config/strategies/paired_binary.yaml",
            scenario_file=f"config/scenarios/{scenario}.yaml",
        )
        assert app.survival.enabled is True
        assert app.survival.target_policy.mode == "dynamic"


def test_preflight_rejects_multi_enforce(monkeypatch) -> None:
    app = load_app_config(
        repo_root=REPO,
        strategy_file="config/strategies/paired_binary.yaml",
        scenario_file="config/scenarios/live_paired_binary_phase1_trailing_enforce.yaml",
    )
    # Simulate accidental dual enforce by mutating stall to enforce as well.
    from dataclasses import replace

    stall = replace(app.survival.stall_exit, enforcement_mode="enforce")
    survival = replace(app.survival, stall_exit=stall)
    app = replace(app, survival=survival)

    errors = validate_phase1_live_app(app, scenario_name="live_paired_binary_phase1_trailing_enforce")
    assert any("only one survival enforce module" in e for e in errors)


def test_preflight_rejects_economics_enforce(monkeypatch) -> None:
    app = load_app_config(
        repo_root=REPO,
        strategy_file="config/strategies/paired_binary.yaml",
        scenario_file="config/scenarios/live_paired_binary_phase1_advisory.yaml",
    )
    from dataclasses import replace

    econ = replace(app.survival.economics, enforcement_mode="enforce")
    survival = replace(app.survival, economics=econ)
    app = replace(app, survival=survival)

    errors = validate_phase1_live_app(app, scenario_name="live_paired_binary_phase1_advisory")
    assert any("economics.enforcement_mode=enforce is not allowed" in e for e in errors)
