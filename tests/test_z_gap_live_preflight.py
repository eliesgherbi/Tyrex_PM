"""Live preflight validator tests (A0.8)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tyrex_pm.runtime.config import parse_app_config
from tyrex_pm.runtime.z_gap_live_preflight import (
    validate_calibration_review,
    validate_market_readiness,
    validate_ptb_attestation_fresh,
    validate_z_gap_live_scenario,
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
        "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
        "market_data": {"enabled": True},
        "strategy_lifecycle": {"mode": "market_aware", "max_runtime_s": None},
    }


def _strategy(**over) -> dict:
    now = time.time()
    zg = {
        "entry_mode": "enforce",
        "market_id": "btc_5m_20260709_1200",
        "condition_id": "0xabc",
        "yes_token_id": "111",
        "no_token_id": "222",
        "event_start_ts": now + 120,
        "event_end_ts": now + 420,
        "sizing": {"mode": "fixed_usd", "max_usd": "5", "min_shares": "5"},
        "exit": {
            "z_stop": "0.25",
            "stop_confirm_s": 1,
            "flatten_before_event_end_s": 20,
            "retry_interval_ms": 1000,
            "max_exit_attempts": 5,
        },
        **over,
    }
    return {"kind": "z_gap", "enabled": True, "z_gap": zg}


def _write_artifacts(path: Path, *, market_id: str = "btc_5m_20260709_1200") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "fee_curve_spike.json").write_text(json.dumps({"outcome": "full_curve_available", "fd": {"r": 0.07, "e": 1}}), encoding="utf-8")
    (path / "binance_connectivity.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
    (path / "ptb_attestation.json").write_text(
        json.dumps({"attestation_pass": True, "ptb_error_bps": "0.1", "golden_fixture_only": False, "enforce_unlock_allowed": True}),
        encoding="utf-8",
    )
    (path / "clock_sanity.json").write_text(
        json.dumps({"sync_status": "synced", "enforce_gate_pass": True, "time_authority_uncertainty_ms": 20.0}),
        encoding="utf-8",
    )
    (path / "calibration_lite_review.json").write_text(
        json.dumps({"reviewed": True, "operator_signoff": True, "status": "accepted_for_tiny_live"}),
        encoding="utf-8",
    )
    (path / "operator_enforce_approval.json").write_text(
        json.dumps(
            {
                "approved": True,
                "market_id": market_id,
                "maximum_usd": "5",
                "one_trade_only": True,
                "expiration_ts": time.time() + 3600,
            }
        ),
        encoding="utf-8",
    )


def test_missing_ptb_attestation_blocks() -> None:
    errs = validate_ptb_attestation_fresh({"golden_fixture_only": True, "attestation_pass": True})
    assert errs


def test_calibration_review_requires_signoff() -> None:
    assert validate_calibration_review({"reviewed": False})


def test_hold_to_resolution_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    art = tmp_path / "gates"
    _write_artifacts(art)
    monkeypatch.setenv("Z_GAP_PREFLIGHT_DIR", str(art))
    app = parse_app_config(risk=_risk(), strategy=_strategy(), runtime=_runtime())
    result = validate_z_gap_live_scenario(
        app,
        artifacts_dir=art,
        raw_strategy_z_gap={"hold_to_resolution": {"enabled": True}},
    )
    assert any("hold_to_resolution" in e for e in result.errors)


def test_non_terminal_persisted_state_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    art = tmp_path / "gates"
    _write_artifacts(art)
    monkeypatch.setenv("Z_GAP_PREFLIGHT_DIR", str(art))
    (art / "lifecycle_state.json").write_text(json.dumps({"phase": "ACTIVE"}), encoding="utf-8")
    app = parse_app_config(risk=_risk(), strategy=_strategy(), runtime=_runtime())
    result = validate_z_gap_live_scenario(app, artifacts_dir=art, lifecycle_state_path=art / "lifecycle_state.json")
    assert any("persisted lifecycle" in e for e in result.errors)


def test_insufficient_prestart_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    art = tmp_path / "gates"
    _write_artifacts(art)
    monkeypatch.setenv("Z_GAP_PREFLIGHT_DIR", str(art))
    now = time.time()
    app = parse_app_config(
        risk=_risk(),
        strategy=_strategy(event_start_ts=now + 5, event_end_ts=now + 300),
        runtime=_runtime(),
    )
    assert app.z_gap is not None
    errs = validate_market_readiness(app.z_gap, now_ts=now)
    assert any("prestart" in e for e in errs)
