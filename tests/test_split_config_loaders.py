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


def _strategy_doc(**kwargs) -> dict:
    base = {
        "guru_wallet_address": "0x1234567890123456789012345678901234567890",
        "token_filter": {"enabled": False, "allowlisted_token_ids": []},
        "copy_scale": 0.5,
    }
    base.update(kwargs)
    return base


def test_load_strategy_unfiltered(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(_strategy_doc()), encoding="utf-8")
    s = load_strategy_settings(p)
    assert not s.token_filter.enabled
    assert s.token_filter.allowlisted_token_ids == ()
    assert s.copy_scale == 0.5


def test_load_strategy_filtered_nonempty(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text(
        yaml.safe_dump(
            _strategy_doc(
                token_filter={
                    "enabled": True,
                    "allowlisted_token_ids": ["1", "2"],
                }
            )
        ),
        encoding="utf-8",
    )
    s = load_strategy_settings(p)
    assert s.token_filter.enabled
    assert s.token_filter.allowlisted_token_ids == ("1", "2")


def test_strategy_rejects_filtered_empty_list(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text(
        yaml.safe_dump(
            _strategy_doc(
                token_filter={"enabled": True, "allowlisted_token_ids": []},
            )
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="non-empty"):
        load_strategy_settings(p)


def test_strategy_rejects_missing_token_filter(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "guru_wallet_address": "0x1234567890123456789012345678901234567890",
                "copy_scale": 1.0,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="token_filter"):
        load_strategy_settings(p)


def test_strategy_rejects_bad_wallet(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "guru_wallet_address": "0xabc",
                "token_filter": {"enabled": False, "allowlisted_token_ids": []},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="guru_wallet_address"):
        load_strategy_settings(p)


def test_strategy_rejects_duplicate_tokens_when_filtered(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text(
        yaml.safe_dump(
            _strategy_doc(
                token_filter={"enabled": True, "allowlisted_token_ids": ["1", "1"]},
            )
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
    assert rs.capital_gate_enabled is False
    assert rs.max_account_snapshot_age_seconds == 30.0

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
    assert live.guru_state_path == "var/guru_watermark.json"
    assert live.guru_activity_limit == 200
    assert live.guru_startup_backfill_seconds == 0.0
    assert live.guru_max_activity_pages_per_poll == 4


def test_runtime_rejects_bad_mode(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    p.write_text(
        yaml.safe_dump({"trader_id": "A-001", "execution_mode": "paper"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="execution_mode"):
        load_runtime_settings(p)


def test_runtime_nautilus_empty_ids_requires_framework_submit(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "A-001",
                "execution_mode": "live",
                "polymarket_nautilus_live": True,
                "polymarket_instrument_ids": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="polymarket_framework_submit"):
        load_runtime_settings(p)


def test_runtime_zero_bootstrap_guru_framework_implies_dynamic(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "A-001",
                "execution_mode": "live",
                "polymarket_nautilus_live": True,
                "polymarket_framework_submit": True,
                "polymarket_instrument_ids": [],
                "polymarket_startup_token_warmup_max": 0,
            }
        ),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.polymarket_instrument_ids == ()
    assert r.polymarket_dynamic_instruments is True
    assert r.polymarket_token_to_instrument == ()
    assert r.polymarket_startup_token_warmup_max == 0


def test_runtime_defaults_framework_submit_and_token_map(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    p.write_text(
        yaml.safe_dump({"trader_id": "A-001", "execution_mode": "live"}),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.polymarket_framework_submit is False
    assert r.polymarket_token_to_instrument == ()
    assert r.polymarket_dynamic_instruments is False
    assert r.polymarket_dynamic_max_activations == 32
    assert r.polymarket_startup_token_warmup_max == 32


def test_runtime_framework_submit_requires_nautilus(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "A-001",
                "execution_mode": "live",
                "polymarket_nautilus_live": False,
                "polymarket_framework_submit": True,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="polymarket_framework_submit requires"):
        load_runtime_settings(p)


def test_runtime_token_map_derived_from_instruments(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "A-001",
                "execution_mode": "live",
                "polymarket_nautilus_live": True,
                "polymarket_instrument_ids": ["0xabc-12345.POLYMARKET"],
                "polymarket_framework_submit": True,
            }
        ),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert ("12345", "0xabc-12345.POLYMARKET") in r.polymarket_token_to_instrument


def test_runtime_dynamic_instruments_requires_framework_submit(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "A-001",
                "execution_mode": "live",
                "polymarket_nautilus_live": True,
                "polymarket_instrument_ids": ["0xabc-12345.POLYMARKET"],
                "polymarket_dynamic_instruments": True,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="polymarket_dynamic_instruments requires"):
        load_runtime_settings(p)


def test_runtime_dynamic_instruments_ok_with_framework(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "A-001",
                "execution_mode": "live",
                "polymarket_nautilus_live": True,
                "polymarket_instrument_ids": ["0xabc-12345.POLYMARKET"],
                "polymarket_framework_submit": True,
                "polymarket_dynamic_instruments": True,
                "polymarket_dynamic_max_activations": 8,
            }
        ),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.polymarket_dynamic_instruments is True
    assert r.polymarket_dynamic_max_activations == 8


def test_runtime_polymarket_nautilus_ok_when_live_with_ids(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "A-001",
                "execution_mode": "live",
                "polymarket_nautilus_live": True,
                "polymarket_instrument_ids": ["0xabc-0xdef.POLYMARKET"],
            }
        ),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.polymarket_nautilus_live is True
    assert r.polymarket_instrument_ids == ("0xabc-0xdef.POLYMARKET",)
