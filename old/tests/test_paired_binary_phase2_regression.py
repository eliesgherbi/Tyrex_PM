"""Phase 2 regression: lifecycle block absent preserves legacy max_runtime behavior."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.ids import RunId
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_PAIRED_BINARY_OPEN_EXPOSURE_AT_SHUTDOWN
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import SurvivalConfig, parse_app_config
from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
from tyrex_pm.runtime.strategy_lifecycle import parse_strategy_lifecycle_config
from tyrex_pm.strategies.paired_binary.exit_engine import ensure_pnl_budgets
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase
from paired_binary_shutdown_helpers import (
    app_cfg,
    both_legs_active_state,
    coord_with_books,
    facts_from_sink,
    risk_cfg,
    seed_both_legs,
)


def test_strategy_lifecycle_absent_means_disabled() -> None:
    policy = parse_strategy_lifecycle_config(None)
    assert policy.enabled is False


def test_survival_defaults_disabled() -> None:
    app = app_cfg()
    assert app.survival.enabled is False


def test_survival_defaults_include_simplified_modules() -> None:
    from tyrex_pm.runtime.config import SurvivalConfig

    cfg = SurvivalConfig(enabled=True)
    assert cfg.survivor_floor.enforcement_mode == "advisory"
    assert cfg.trailing_stop.activation_mode == "loss_recovered"
    assert cfg.stall_exit.enabled is False


@pytest.mark.asyncio
async def test_legacy_max_runtime_force_flatten_without_lifecycle_block(tmp_path: Path) -> None:
    """Existing Phase 2 scenarios without strategy_lifecycle still force-flatten at max_runtime."""
    app = app_cfg(max_runtime_s=0.01, tick_interval_s=0.005)
    assert app.runtime.strategy_lifecycle.enabled is False
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    seed_both_legs(coord)
    state = both_legs_active_state()
    ensure_pnl_budgets(state, cfg)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        await run_paired_binary_loop(
            app=app,
            run_id=RunId("phase2-regression-legacy"),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )
        facts = facts_from_sink(sink)
    types = {f["fact_type"] for f in facts}
    assert FACT_TYPE_PAIRED_BINARY_OPEN_EXPOSURE_AT_SHUTDOWN in types
    assert state.phase in {PairedBinaryPhase.DONE, PairedBinaryPhase.FAILED}


def test_survival_enforcement_retry_defaults_disabled() -> None:
    from tyrex_pm.runtime.config import SurvivalEnforcementConfig

    cfg = SurvivalEnforcementConfig()
    assert cfg.retry_quality_rejects is False


def test_survival_enforcement_order_policy_defaults_conservative() -> None:
    from tyrex_pm.runtime.config import SurvivalConfig

    cfg = SurvivalConfig(enabled=False)
    assert cfg.enabled is False
    op = cfg.enforcement.order_policy
    assert op.mode == "fak_retry"
    assert op.managed_rest_enabled is False
    assert op.post_only_for_survival_exit is False


def test_phase1_tiny_scenario_requires_preflight_before_live() -> None:
    from pathlib import Path

    from scripts.preflight_phase1_live_scenario import validate_phase1_live_app
    from tyrex_pm.runtime.config import load_app_config

    repo = Path(__file__).resolve().parents[1]
    app = load_app_config(
        repo_root=repo,
        strategy_file="config/strategies/paired_binary.yaml",
        scenario_file="config/scenarios/live_paired_binary_phase1_tiny.yaml",
    )
    assert app.survival.enabled is True
    assert app.survival.reachability.enforcement_mode == "advisory"
    assert app.runtime.paired_binary.stop_background_tasks_after_strategy_done is True
    errors = validate_phase1_live_app(app, now_ts=__import__("time").time())
    assert errors, "placeholder scenario must fail preflight until operator pins metadata"


def test_runtime_config_without_strategy_lifecycle_block() -> None:
    from tyrex_pm.runtime.config import StrategyLifecycleConfig

    cfg = StrategyLifecycleConfig()
    assert cfg.enabled is False
