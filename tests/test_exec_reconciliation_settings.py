"""Live execution reconciliation defaults (Nautilus LiveExecEngineConfig wiring)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tyrex_pm.config.loaders import load_runtime_settings


def test_live_omitted_exec_position_check_defaults_45(tmp_path: Path) -> None:
    p = tmp_path / "live.yaml"
    p.write_text(
        yaml.safe_dump({"trader_id": "T-001", "execution_mode": "live"}),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.exec_position_check_interval_secs == 45.0
    assert r.exec_open_check_interval_secs == 20.0


def test_live_exec_position_check_null_disables(tmp_path: Path) -> None:
    p = tmp_path / "live.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "T-001",
                "execution_mode": "live",
                "exec_position_check_interval_seconds": None,
            },
        ),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.exec_position_check_interval_secs is None


def test_shadow_exec_position_check_off(tmp_path: Path) -> None:
    p = tmp_path / "live.yaml"
    p.write_text(
        yaml.safe_dump({"trader_id": "T-001", "execution_mode": "shadow"}),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.exec_position_check_interval_secs is None
    assert r.exec_open_check_interval_secs is None


def test_live_wallet_position_warmup_defaults_128(tmp_path: Path) -> None:
    p = tmp_path / "live.yaml"
    p.write_text(
        yaml.safe_dump({"trader_id": "T-001", "execution_mode": "live"}),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.polymarket_wallet_position_warmup_max == 128


def test_shadow_wallet_position_warmup_zero(tmp_path: Path) -> None:
    p = tmp_path / "live.yaml"
    p.write_text(
        yaml.safe_dump({"trader_id": "T-001", "execution_mode": "shadow"}),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.polymarket_wallet_position_warmup_max == 0


def test_live_exec_position_check_custom(tmp_path: Path) -> None:
    p = tmp_path / "live.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "T-001",
                "execution_mode": "live",
                "exec_position_check_interval_seconds": 30,
            },
        ),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.exec_position_check_interval_secs == 30.0
    assert r.exec_open_check_interval_secs == 20.0


def test_live_exec_open_check_null_disables(tmp_path: Path) -> None:
    p = tmp_path / "live.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "T-001",
                "execution_mode": "live",
                "exec_open_check_interval_seconds": None,
            },
        ),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.exec_open_check_interval_secs is None
    assert r.exec_position_check_interval_secs == 45.0


def test_live_exec_open_check_custom(tmp_path: Path) -> None:
    p = tmp_path / "live.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "T-001",
                "execution_mode": "live",
                "exec_open_check_interval_seconds": 12,
            },
        ),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.exec_open_check_interval_secs == 12.0


def test_live_exec_open_check_invalid_raises(tmp_path: Path) -> None:
    p = tmp_path / "live.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "T-001",
                "execution_mode": "live",
                "exec_open_check_interval_seconds": 0,
            },
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exec_open_check_interval_seconds"):
        load_runtime_settings(p)
