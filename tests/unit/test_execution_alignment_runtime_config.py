"""Phase 5 — execution alignment runtime YAML (``execution_truth_alignment.md``)."""

from __future__ import annotations

from pathlib import Path

import yaml

from tyrex_pm.config.loaders import load_runtime_settings


def test_execution_alignment_defaults(tmp_path: Path) -> None:
    p = tmp_path / "r.yaml"
    p.write_text(
        yaml.safe_dump({"trader_id": "A-001", "execution_mode": "live"}),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.polymarket_use_data_api_for_positions is False
    assert r.live_exec_open_check_open_only is None


def test_execution_alignment_yaml_overrides(tmp_path: Path) -> None:
    p = tmp_path / "r.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "A-001",
                "execution_mode": "live",
                "polymarket_use_data_api_for_positions": True,
                "live_exec_open_check_open_only": False,
            },
        ),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.polymarket_use_data_api_for_positions is True
    assert r.live_exec_open_check_open_only is False


def test_live_exec_open_check_open_only_null(tmp_path: Path) -> None:
    p = tmp_path / "r.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "A-001",
                "execution_mode": "live",
                "live_exec_open_check_open_only": None,
            },
        ),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.live_exec_open_check_open_only is None
