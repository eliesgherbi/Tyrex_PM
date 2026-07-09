"""Phase 1 simplified scenario profile validation."""

from __future__ import annotations

from pathlib import Path

import yaml

from scripts.preflight_phase1_live_scenario import _enforce_mode_count
from tyrex_pm.runtime.config import load_app_config

REPO = Path(__file__).resolve().parents[1]
SCENARIOS = ("live_paired_binary_phase1_advisory", "live_paired_binary_phase1_trailing_enforce")


def _load(scenario: str):
    return load_app_config(
        repo_root=REPO,
        strategy_file="config/strategies/paired_binary.yaml",
        scenario_file=f"config/scenarios/{scenario}.yaml",
    )


def test_simplified_profiles_have_no_stall_or_target_policy() -> None:
    for scenario in SCENARIOS:
        raw = yaml.safe_load((REPO / "config" / "scenarios" / f"{scenario}.yaml").read_text())
        survival = raw["runtime"]["survival"]
        assert "stall_exit" not in survival
        assert "target_policy" not in survival


def test_simplified_profiles_have_floor_recovery_trailing() -> None:
    for scenario in SCENARIOS:
        app = _load(scenario)
        assert app.survival.survivor_floor.enabled is True
        assert app.survival.trailing_stop.activation_mode == "loss_recovered"
        assert app.survival.recovery_level.desired_buffer is not None


def test_trailing_enforce_single_enforce_module() -> None:
    app = _load("live_paired_binary_phase1_trailing_enforce")
    count, enforced = _enforce_mode_count(app)
    assert count == 1
    assert enforced == ["trailing_stop"]


def test_advisory_profile_all_advisory() -> None:
    app = _load("live_paired_binary_phase1_advisory")
    count, enforced = _enforce_mode_count(app)
    assert count == 0
    assert enforced == []
