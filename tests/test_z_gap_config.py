"""Tests for Z-Gap config parsing and enforce gate wiring (A0.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tyrex_pm.runtime.config import (
    ConfigError,
    Z_GAP_ENTRY_MODE_ENFORCE,
    Z_GAP_ENTRY_MODE_OBSERVE_ONLY,
    parse_app_config,
)


def _risk() -> dict:
    return {
        "notional": {"min_usd": "0.01", "max_usd": "5", "max_policy": "deny"},
        "deployment": {"token_cap_usd": "25", "portfolio_cap_usd": "100"},
        "venue_min_size": {"enabled": False},
        "capital": {"enabled": False, "max_wallet_age_s": 120},
        "concurrency": {"max_orders_in_flight": 4},
        "readiness": {
            "require_wallet_sync": False,
            "max_wallet_age_s_live": 120,
            "require_heartbeat_live": False,
            "require_user_ws_live": False,
        },
    }


def _runtime() -> dict:
    return {
        "execution_mode": "live",
        "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
        "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
        "market_data": {"enabled": True},
        "strategy_lifecycle": {"mode": "market_aware", "max_runtime_s": None},
    }


def _strategy(**zg_over) -> dict:
    zg = {
        "market_id": "btc_5m_20260701_2045",
        "condition_id": "0xabc",
        "yes_token_id": "111",
        "no_token_id": "222",
        "event_start_ts": 1782938400,
        "event_end_ts": 1782938700,
        **zg_over,
    }
    return {"kind": "z_gap", "enabled": True, "z_gap": zg}


def _write_gate_artifacts(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "fee_curve_spike.json").write_text(
        json.dumps({"outcome": "full_curve_available", "fd": {"r": 0.07, "e": 1}}),
        encoding="utf-8",
    )
    (path / "binance_connectivity.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
    (path / "ptb_attestation.json").write_text(
        json.dumps(
            {
                "attestation_pass": True,
                "ptb_error_bps": "0.1",
                "golden_fixture_only": False,
                "enforce_unlock_allowed": True,
            }
        ),
        encoding="utf-8",
    )
    (path / "clock_sanity.json").write_text(
        json.dumps(
            {
                "sync_status": "synced",
                "enforce_gate_pass": True,
                "time_authority_uncertainty_ms": 20.0,
                "clock_drift_ms": 12.0,
            }
        ),
        encoding="utf-8",
    )
    (path / "calibration_lite_review.json").write_text(
        json.dumps({"reviewed": True, "operator_signoff": True}),
        encoding="utf-8",
    )
    (path / "operator_enforce_approval.json").write_text(
        json.dumps({"approved": True}),
        encoding="utf-8",
    )


def test_z_gap_kind_parses() -> None:
    app = parse_app_config(risk=_risk(), strategy=_strategy(), runtime=_runtime())
    assert app.strategy_kind == "z_gap"
    assert app.z_gap is not None
    assert app.z_gap.market_id == "btc_5m_20260701_2045"


def test_z_gap_default_entry_mode_observe_only() -> None:
    app = parse_app_config(risk=_risk(), strategy=_strategy(), runtime=_runtime())
    assert app.z_gap is not None
    assert app.z_gap.entry_mode == Z_GAP_ENTRY_MODE_OBSERVE_ONLY


def test_z_gap_invalid_entry_mode_fails() -> None:
    with pytest.raises(ConfigError, match="entry_mode"):
        parse_app_config(
            risk=_risk(),
            strategy=_strategy(entry_mode="trade_all_day"),
            runtime=_runtime(),
        )


def test_z_gap_enforce_without_preflight_gates_blocked() -> None:
    with pytest.raises(ConfigError, match="preflight gates"):
        parse_app_config(
            risk=_risk(),
            strategy=_strategy(entry_mode=Z_GAP_ENTRY_MODE_ENFORCE),
            runtime=_runtime(),
        )


def test_z_gap_enforce_with_all_gates_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gates = tmp_path / "z_gap_gates"
    _write_gate_artifacts(gates)
    monkeypatch.setenv("Z_GAP_PREFLIGHT_DIR", str(gates))
    app = parse_app_config(
        risk=_risk(),
        strategy=_strategy(entry_mode=Z_GAP_ENTRY_MODE_ENFORCE),
        runtime=_runtime(),
    )
    assert app.z_gap is not None
    assert app.z_gap.entry_mode == Z_GAP_ENTRY_MODE_ENFORCE
