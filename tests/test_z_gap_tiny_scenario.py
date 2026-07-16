"""Tiny scenario config tests (A0.8)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from tyrex_pm.runtime.config import load_app_config


def test_live_z_gap_tiny_yaml_parses() -> None:
    path = Path("config/scenarios/live_z_gap_tiny.yaml")
    if not path.is_file():
        pytest.skip("live_z_gap_tiny.yaml not present")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    zg = doc["strategy"]["z_gap"]
    assert zg["entry_mode"] == "enforce"
    assert zg["sizing"]["max_usd"] == "5"
    assert zg["live_validation"]["maximum_entry_attempts"] == 1


def test_shadow_scenario_parses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tyrex_pm.strategies.z_gap.shadow_harness import write_preflight_artifacts

    art = tmp_path / "gates"
    write_preflight_artifacts(art, market_id="btc_5m_20260709_1200")
    monkeypatch.setenv("Z_GAP_PREFLIGHT_DIR", str(art))
    repo = Path(__file__).resolve().parents[1]
    app = load_app_config(
        repo_root=repo,
        strategy_file="config/strategies/z_gap.yaml",
        scenario_file="config/scenarios/shadow_z_gap_enforce_e2e.yaml",
    )
    assert app.z_gap is not None
    assert app.runtime.execution_mode.value == "shadow"
