"""Phase 1 scenario profile parsing and preflight rules."""

from __future__ import annotations

from pathlib import Path

from scripts.preflight_phase1_live_scenario import (
    _enforce_mode_count,
    _phase1_profile_kind,
)
from tyrex_pm.runtime.config import load_app_config

REPO = Path(__file__).resolve().parents[1]


def _load(scenario: str):
    return load_app_config(
        repo_root=REPO,
        strategy_file="config/strategies/paired_binary.yaml",
        scenario_file=f"config/scenarios/{scenario}.yaml",
    )


def test_advisory_profile_all_advisory() -> None:
    app = _load("live_paired_binary_phase1_advisory")
    count, enforced = _enforce_mode_count(app)
    assert count == 0
    assert enforced == []
    assert _phase1_profile_kind("live_paired_binary_phase1_advisory") == "advisory"


def test_trailing_enforce_profile_single_enforce() -> None:
    app = _load("live_paired_binary_phase1_trailing_enforce")
    count, enforced = _enforce_mode_count(app)
    assert count == 1
    assert enforced == ["trailing_stop"]
    assert _phase1_profile_kind("live_paired_binary_phase1_trailing_enforce") == "trailing_enforce"


def test_stall_enforce_profile_single_enforce() -> None:
    app = _load("live_paired_binary_phase1_stall_enforce")
    count, enforced = _enforce_mode_count(app)
    assert count == 1
    assert enforced == ["stall_exit"]


def test_advisory_profile_rejects_enforce_modes_via_count() -> None:
    app = _load("live_paired_binary_phase1_trailing_enforce")
    count, _ = _enforce_mode_count(app)
    assert count == 1
    assert _phase1_profile_kind("live_paired_binary_phase1_advisory") == "advisory"
