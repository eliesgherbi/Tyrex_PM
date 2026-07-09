"""Phase 1 M6 — survival kill switch unit tests."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.runtime.config import SurvivalConfig, SurvivalKillSwitchConfig
from tyrex_pm.survival.kill_switches import (
    ACTION_DENY_ENTRY,
    ACTION_FORCE_FLATTEN_PAIR,
    ACTION_HARD_STOP,
    ACTION_PAUSE_STRATEGY,
    KillSwitchManager,
)


def _mgr(**overrides) -> KillSwitchManager:
    cfg = SurvivalKillSwitchConfig(enabled=True, **overrides)
    return KillSwitchManager(cfg)


def test_per_pair_max_loss_triggers_force_flatten_and_deny_entry() -> None:
    mgr = _mgr(per_pair_max_loss_usd=Decimal("0.50"))
    mgr.record_lifecycle_terminal("DONE", Decimal("-0.60"))
    decision = mgr.check(owner_id="owner1", pair_id="m1")
    assert decision.triggered is True
    assert decision.switch_name == "per_pair_max_loss_usd"
    assert decision.action == ACTION_FORCE_FLATTEN_PAIR
    actions = {d.action for d in mgr.triggered_decisions(owner_id="owner1", pair_id="m1")}
    assert ACTION_DENY_ENTRY in actions
    assert ACTION_FORCE_FLATTEN_PAIR in actions


def test_daily_max_loss_triggers_deny_entry() -> None:
    mgr = _mgr(daily_max_loss_usd=Decimal("1.00"), per_pair_max_loss_usd=Decimal("100"))
    mgr.record_lifecycle_terminal("DONE", Decimal("-1.10"))
    decision = mgr.check(owner_id="owner1", pair_id="m1")
    assert decision.triggered is True
    assert decision.switch_name == "daily_max_loss_usd"
    assert decision.action == ACTION_DENY_ENTRY


def test_max_failed_lifecycle_count_triggers_pause_strategy() -> None:
    mgr = _mgr(max_failed_lifecycle_count=2)
    mgr.record_lifecycle_terminal("FAILED", Decimal("0"))
    mgr.record_lifecycle_terminal("FAILED", Decimal("0"))
    decision = mgr.check(owner_id="owner1", pair_id="m1")
    assert decision.triggered is True
    assert decision.switch_name == "max_failed_lifecycle_count"
    assert decision.action == ACTION_PAUSE_STRATEGY


def test_max_consecutive_no_entry_triggers_pause() -> None:
    mgr = _mgr(max_consecutive_no_entry=3)
    mgr.record_no_entry_run()
    mgr.record_no_entry_run()
    mgr.record_no_entry_run()
    decision = mgr.check(owner_id="owner1", pair_id="m1")
    assert decision.triggered is True
    assert decision.switch_name == "max_consecutive_no_entry"
    assert decision.action == ACTION_PAUSE_STRATEGY


def test_max_manual_intervention_count_triggers_hard_stop() -> None:
    mgr = _mgr(max_manual_intervention_count=2)
    mgr.record_manual_intervention()
    mgr.record_manual_intervention()
    decision = mgr.check(owner_id="owner1", pair_id="m1")
    assert decision.triggered is True
    assert decision.switch_name == "max_manual_intervention_count"
    assert decision.action == ACTION_HARD_STOP
    assert mgr.state.hard_stop_active is True


def test_daily_reset_clears_counters(tmp_path: Path) -> None:
    path = tmp_path / "owner1.json"
    cfg = SurvivalKillSwitchConfig(enabled=True, persist_daily=True, max_consecutive_no_entry=5)
    mgr = KillSwitchManager(cfg, state_path=path)
    mgr.record_no_entry_run()
    mgr.record_no_entry_run()
    assert mgr.state.consecutive_no_entry == 2
    mgr.reset_daily_if_needed(1_700_000_000.0)
    assert mgr.state.consecutive_no_entry == 0
    assert path.is_file()


def test_no_hard_cancel_all_in_kill_switch_modules() -> None:
    repo = Path(__file__).resolve().parents[1]
    for rel in (
        "src/tyrex_pm/survival/kill_switches.py",
        "src/tyrex_pm/survival/kill_switch_runtime.py",
    ):
        text = (repo / rel).read_text(encoding="utf-8")
        assert "cancel_all" not in text.lower()
        assert "cancel_every" not in text.lower()


def test_survival_disabled_kill_switches_inactive() -> None:
    cfg = SurvivalConfig(enabled=False)
    assert cfg.kill_switches.enabled is False
    mgr = KillSwitchManager(cfg.kill_switches)
    mgr.record_manual_intervention()
    decision = mgr.check(owner_id="o", pair_id="p")
    assert decision.triggered is False


@pytest.mark.asyncio
async def test_force_flatten_uses_reduce_only_shutdown_path(tmp_path: Path) -> None:
    from dataclasses import replace

    from tyrex_pm.core.ids import RunId
    from tyrex_pm.execution.adapters import ShadowOMS
    from tyrex_pm.reporting.schema_v2 import FACT_TYPE_PAIRED_BINARY_SHUTDOWN_FORCE_FLATTEN_STARTED
    from tyrex_pm.reporting.sinks.jsonl import JsonlSink
    from tyrex_pm.runtime.config import SurvivalConfig, SurvivalKillSwitchConfig
    from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
    from tyrex_pm.strategies.paired_binary.exit_engine import ensure_pnl_budgets
    from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase
    from paired_binary_shutdown_helpers import (
        app_cfg,
        both_legs_active_state,
        coord_with_books,
        facts_from_sink,
        seed_both_legs,
    )
    from tyrex_pm.survival.kill_switches import KillSwitchManager, kill_switch_state_path

    app = replace(
        app_cfg(max_runtime_s=600, tick_interval_s=0.005),
        survival=SurvivalConfig(
            enabled=True,
            kill_switches=SurvivalKillSwitchConfig(
                enabled=True,
                per_pair_max_loss_usd=Decimal("0.01"),
                persist_daily=True,
            ),
        ),
    )
    cfg = app.paired_binary
    assert cfg is not None
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ks_path = kill_switch_state_path(state_dir, cfg.owner_id)
    ks_path.parent.mkdir(parents=True, exist_ok=True)
    mgr = KillSwitchManager(app.survival.kill_switches, state_path=ks_path)
    mgr.record_lifecycle_terminal("DONE", Decimal("-0.50"))

    coord = coord_with_books(tmp_path)
    seed_both_legs(coord)
    state = both_legs_active_state()
    ensure_pnl_budgets(state, cfg)

    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        await run_paired_binary_loop(
            app=app,
            run_id=RunId("ks-force-flatten"),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=state_dir,
        )
        facts = facts_from_sink(sink)
    types = {f["fact_type"] for f in facts}
    assert FACT_TYPE_PAIRED_BINARY_SHUTDOWN_FORCE_FLATTEN_STARTED in types or "kill_switch_triggered" in types
    assert state.phase in {PairedBinaryPhase.DONE, PairedBinaryPhase.FAILED}


@pytest.mark.asyncio
async def test_survival_disabled_no_kill_switch_behavior_change(tmp_path: Path) -> None:
    from tyrex_pm.core.ids import RunId
    from tyrex_pm.execution.adapters import ShadowOMS
    from tyrex_pm.reporting.schema_v2 import FACT_TYPE_PAIRED_BINARY_OPEN_EXPOSURE_AT_SHUTDOWN
    from tyrex_pm.reporting.sinks.jsonl import JsonlSink
    from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
    from tyrex_pm.strategies.paired_binary.exit_engine import ensure_pnl_budgets
    from paired_binary_shutdown_helpers import (
        app_cfg,
        both_legs_active_state,
        coord_with_books,
        facts_from_sink,
        seed_both_legs,
    )

    app = app_cfg(max_runtime_s=0.01, tick_interval_s=0.005)
    assert app.survival.enabled is False
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    seed_both_legs(coord)
    state = both_legs_active_state()
    ensure_pnl_budgets(state, cfg)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        await run_paired_binary_loop(
            app=app,
            run_id=RunId("phase2-no-ks"),
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
    assert "kill_switch_triggered" not in types
