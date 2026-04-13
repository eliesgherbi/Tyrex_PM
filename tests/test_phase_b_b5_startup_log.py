"""B5: Phase B startup summary line (informational only)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tyrex_pm.config.loaders import RiskSettings
from tyrex_pm.runtime.guru_compose import build_guru_trading_node
from tyrex_pm.runtime.phase_b_startup import phase_b_startup_summary_line


def _minimal_risk(**kw: object) -> RiskSettings:
    base = {
        "max_notional_usd_per_order": 1000.0,
        "max_token_notional_usd_open": float("inf"),
        "kill_switch": False,
        "fail_on_missing_price_for_notional": True,
    }
    base.update(kw)
    return RiskSettings(**base)  # type: ignore[arg-type]


def test_phase_b_startup_summary_includes_flags() -> None:
    from tyrex_pm.config.loaders import RuntimeSettings

    risk = _minimal_risk(
        max_portfolio_notional_usd_open=500_000.0,
        max_concurrent_guru_resting_orders=4,
        collateral_reserve_usd=100.0,
        capital_gate_enabled=True,
        fail_on_unresolved_portfolio_deployment=False,
    )
    runtime = RuntimeSettings(  # type: ignore[call-arg]
        trader_id="T-1",
        execution_mode="live",
        guru_poll_interval_seconds=30.0,
        data_api_base_url="https://data-api.polymarket.com",
        guru_dedup_state_path="var/d.json",
        guru_state_path="var/w.json",
        guru_activity_limit=200,
        guru_startup_backfill_seconds=0.0,
        guru_max_activity_pages_per_poll=4,
        logging_level="INFO",
        clob_host="https://clob.polymarket.com",
        chain_id=137,
        polymarket_instrument_ids=(),
        polymarket_token_to_instrument=(),
        polymarket_dynamic_instruments=False,
        polymarket_dynamic_max_activations=32,
        polymarket_gamma_base_url="https://gamma-api.polymarket.com",
        polymarket_gamma_http_timeout_seconds=15.0,
        polymarket_startup_token_warmup_max=0,
        exec_position_check_interval_secs=None,
        exec_open_check_interval_secs=None,
        polymarket_wallet_position_warmup_max=0,
    )
    line = phase_b_startup_summary_line(risk, runtime, deployment_budget_wired=True)
    assert "framework_truth_eligible=True" in line
    assert "deployment_budget_wired=True" in line
    assert "portfolio_deployment_cap_usd=500000.0" in line
    assert "max_concurrent_guru_resting_orders=4" in line
    assert "fail_on_unresolved_portfolio_deployment=False" in line
    assert "collateral_reserve_usd=100.0" in line
    assert "capital_gate_enabled=True" in line
    assert "exec_position_check_interval_secs=off" in line
    assert "exec_open_check_interval_secs=off" in line
    assert "polymarket_wallet_position_warmup_max=0" in line


def test_build_guru_trading_node_logs_phase_b_line(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Compose emits one INFO line from tyrex_pm.runtime.guru_compose (B5)."""
    import yaml

    from tyrex_pm.config.loaders import (
        load_risk_settings,
        load_runtime_settings,
        load_strategy_settings,
    )

    caplog.set_level(logging.INFO, logger="tyrex_pm.runtime.guru_compose")
    root = Path(__file__).resolve().parent.parent
    strat = load_strategy_settings(root / "config" / "strategy" / "guru_follow.yaml")
    risk = load_risk_settings(root / "config" / "risk" / "guru_follow_risk.yaml")
    live = tmp_path / "live.yaml"
    live.write_text(
        yaml.safe_dump(
            {
                "trader_id": "TEST-B5-001",
                "execution_mode": "shadow",
                "guru_poll_interval_seconds": 60.0,
            },
        ),
        encoding="utf-8",
    )
    runtime = load_runtime_settings(live)
    _ = build_guru_trading_node(strat, risk, runtime)
    matches = [
        r.message for r in caplog.records if "tyrex_pm phase_b:" in r.message
    ]
    assert len(matches) == 1
    assert "framework_truth_eligible=" in matches[0]
    assert "exec_open_check_interval_secs=off" in matches[0]
