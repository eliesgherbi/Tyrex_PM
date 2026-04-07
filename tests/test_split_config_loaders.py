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
                "max_notional_usd_per_order": 5,
                "max_token_notional_usd_open": 20,
            }
        ),
        encoding="utf-8",
    )
    rs = load_risk_settings(r)
    assert rs.kill_switch is False
    assert rs.capital_gate_enabled is False
    assert rs.max_account_snapshot_age_seconds == 30.0
    assert rs.fail_on_unresolved_portfolio_deployment is True
    assert rs.max_notional_policy == "cap"
    assert rs.min_notional_policy == "deny"

    bad_pol = tmp_path / "risk_bad_pol.yaml"
    bad_pol.write_text(
        yaml.safe_dump(
            {
                "max_notional_usd_per_order": 1,
                "max_token_notional_usd_open": 20,
                "max_notional_policy": "nope",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="max_notional_policy"):
        load_risk_settings(bad_pol)

    for bad_key, bad_val, needle in (
        ("max_order_quantity", 1, "max_order_quantity"),
        ("portfolio_sizing_mode", "fancy", "portfolio_sizing_mode"),
        ("fail_on_unresolved_portfolio_exposure", True, "fail_on_unresolved_portfolio_exposure"),
    ):
        bad = tmp_path / f"risk_bad_{bad_key}.yaml"
        bad.write_text(
            yaml.safe_dump(
                {
                    bad_key: bad_val,
                    "max_notional_usd_per_order": 1,
                    "max_token_notional_usd_open": 20,
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=needle):
            load_risk_settings(bad)

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


def test_runtime_rejects_obsolete_polymarket_nautilus_live_key(tmp_path: Path) -> None:
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
    with pytest.raises(ValueError, match="obsolete"):
        load_runtime_settings(p)


def test_runtime_zero_bootstrap_guru_framework_implies_dynamic(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "A-001",
                "execution_mode": "live",
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


def test_runtime_defaults_live_implies_dynamic_when_empty_instruments(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    p.write_text(
        yaml.safe_dump({"trader_id": "A-001", "execution_mode": "live"}),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.polymarket_token_to_instrument == ()
    assert r.polymarket_dynamic_instruments is True
    assert r.polymarket_dynamic_max_activations == 32
    assert r.polymarket_startup_token_warmup_max == 32


def test_runtime_token_map_derived_from_instruments(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "A-001",
                "execution_mode": "live",
                "polymarket_instrument_ids": ["0xabc-12345.POLYMARKET"],
            }
        ),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.polymarket_dynamic_instruments is False
    assert ("12345", "0xabc-12345.POLYMARKET") in r.polymarket_token_to_instrument


def test_runtime_dynamic_instruments_opt_in_with_nonempty_ids(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "A-001",
                "execution_mode": "live",
                "polymarket_instrument_ids": ["0xabc-12345.POLYMARKET"],
                "polymarket_dynamic_instruments": True,
            }
        ),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.polymarket_dynamic_instruments is True


def test_runtime_dynamic_instruments_max_activations(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "A-001",
                "execution_mode": "live",
                "polymarket_instrument_ids": ["0xabc-12345.POLYMARKET"],
                "polymarket_dynamic_instruments": True,
                "polymarket_dynamic_max_activations": 8,
            }
        ),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.polymarket_dynamic_instruments is True
    assert r.polymarket_dynamic_max_activations == 8


def test_runtime_live_with_explicit_instrument_ids(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "A-001",
                "execution_mode": "live",
                "polymarket_instrument_ids": ["0xabc-0xdef.POLYMARKET"],
            }
        ),
        encoding="utf-8",
    )
    r = load_runtime_settings(p)
    assert r.polymarket_instrument_ids == ("0xabc-0xdef.POLYMARKET",)


def test_runtime_dynamic_instruments_invalid_in_shadow(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "trader_id": "A-001",
                "execution_mode": "shadow",
                "polymarket_dynamic_instruments": True,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="polymarket_dynamic_instruments"):
        load_runtime_settings(p)
