"""Application config loading (v1.03)."""

from pathlib import Path

import pytest
import yaml

from tyrex_pm.core.app_config import load_app_config


def test_load_example_config():
    root = Path(__file__).resolve().parent.parent
    cfg = load_app_config(root / "config" / "v1.example.yaml")
    assert cfg.mode == "backtest"
    assert cfg.trader_id == "TYREX-001"


def test_missing_key(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"mode": "live"}), encoding="utf-8")
    with pytest.raises(ValueError, match="trader_id"):
        load_app_config(p)


def test_bad_mode(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text(
        yaml.safe_dump({"mode": "paper", "trader_id": "X"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="mode"):
        load_app_config(p)
