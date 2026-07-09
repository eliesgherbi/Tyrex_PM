"""Phase 1 live scenario preflight tests."""

from __future__ import annotations

import time
from dataclasses import replace
from decimal import Decimal

from scripts.preflight_phase1_live_scenario import validate_phase1_live_app
from tyrex_pm.runtime.config import (
    EconomicsConfig,
    ExecutionMode,
    ReachabilityConfig,
    StallExitConfig,
    SurvivalConfig,
    TrailingStopConfig,
    parse_app_config,
)
from paired_binary_shutdown_helpers import app_cfg, risk_cfg, strategy_cfg


def _phase1_app(*, market_id: str = "btc_5m_20260701_2010", **pb_over):
    now = time.time()
    pb = {
        "market_id": market_id,
        "condition_id": "0xabc123",
        "event_start_ts": now - 60,
        "event_end_ts": now + 240,
        "use_fixture_book": False,
        **pb_over,
    }
    base = app_cfg(max_runtime_s=600)
    return parse_app_config(
        risk=risk_cfg(),
        strategy=strategy_cfg(**pb),
        runtime={
            "execution_mode": "live",
            "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
            "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
            "market_data": {"enabled": True},
            "execution": {"planner": {"enabled": True}},
            "strategy_lifecycle": {"mode": "market_aware", "max_runtime_s": None},
            "survival": {
                "enabled": True,
                "reachability": {"enforcement_mode": "advisory"},
                "stall_exit": {"enforcement_mode": "advisory"},
                "trailing_stop": {"enforcement_mode": "advisory"},
                "economics": {"enforcement_mode": "advisory"},
            },
        },
    )


def test_preflight_rejects_shadow_test_market() -> None:
    app = _phase1_app(market_id="shadow_test_market")
    errors = validate_phase1_live_app(app, now_ts=time.time())
    assert any("market_id" in e for e in errors)


def test_preflight_rejects_missing_event_timestamps() -> None:
    app = _phase1_app(event_start_ts=None, event_end_ts=None)
    errors = validate_phase1_live_app(app, now_ts=time.time())
    assert any("event_start_ts" in e for e in errors)
    assert any("event_end_ts" in e for e in errors)


def test_preflight_accepts_valid_metadata() -> None:
    app = _phase1_app()
    app = replace(
        app,
        survival=SurvivalConfig(
            enabled=True,
            reachability=ReachabilityConfig(enforcement_mode="advisory"),
            stall_exit=StallExitConfig(enforcement_mode="advisory"),
            trailing_stop=TrailingStopConfig(enforcement_mode="advisory"),
            economics=EconomicsConfig(enforcement_mode="advisory"),
        ),
    )
    errors = validate_phase1_live_app(app, now_ts=time.time())
    assert errors == []


def test_default_phase1_scenario_fails_preflight() -> None:
    from pathlib import Path

    from tyrex_pm.runtime.config import load_app_config

    repo = Path(__file__).resolve().parents[1]
    app = load_app_config(
        repo_root=repo,
        strategy_file="config/strategies/paired_binary.yaml",
        scenario_file="config/scenarios/live_paired_binary_phase1_tiny.yaml",
    )
    errors = validate_phase1_live_app(app, now_ts=time.time())
    assert errors
