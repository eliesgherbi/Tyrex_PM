"""Typed strategy / risk / runtime YAML loaders."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tyrex_pm.config.loaders import (
    load_risk_settings,
    load_runtime_settings,
    load_strategy_settings,
)


def test_load_strategy_minimal(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "guru_wallet_address": "0x1234567890123456789012345678901234567890",
                "allowlisted_token_ids": ["1", "2"],
                "copy_scale": 0.5,
            }
        ),
        encoding="utf-8",
    )
    s = load_strategy_settings(p)
    assert s.guru_wallet_address.startswith("0x")
    assert s.allowlisted_token_ids == ("1", "2")
    assert s.copy_scale == 0.5
    assert s.strategy_dedup_state_path is None


def test_strategy_rejects_bad_wallet(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "guru_wallet_address": "0xabc",
                "allowlisted_token_ids": ["1"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="guru_wallet_address"):
        load_strategy_settings(p)


def test_strategy_rejects_duplicate_tokens(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "guru_wallet_address": "0x1234567890123456789012345678901234567890",
                "allowlisted_token_ids": ["1", "1"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_strategy_settings(p)


def test_load_risk_and_runtime(tmp_path: Path) -> None:
    r = tmp_path / "r.yaml"
    r.write_text(
        yaml.safe_dump(
            {
                "max_order_quantity": 10,
                "max_notional_usd_per_order": 5,
                "max_token_notional_usd_open": 20,
            }
        ),
        encoding="utf-8",
    )
    rs = load_risk_settings(r)
    assert rs.max_order_quantity == 10.0
    assert rs.kill_switch is False

    rt = tmp_path / "live.yaml"
    rt.write_text(
        yaml.safe_dump(
            {
                "trader_id": "X-001",
                "execution_mode": "live",
            }
        ),
        encoding="utf-8",
    )
    live = load_runtime_settings(rt)
    assert live.execution_mode == "live"
    assert live.guru_dedup_state_path == "var/guru_dedup.json"


def test_runtime_rejects_bad_mode(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    p.write_text(
        yaml.safe_dump({"trader_id": "A-001", "execution_mode": "paper"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="execution_mode"):
        load_runtime_settings(p)
