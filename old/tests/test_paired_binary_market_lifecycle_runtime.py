"""Integration: market lifecycle runtime in paired_binary loop (Wave A / M0)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.ids import RunId
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_PAIRED_BINARY_OPEN_EXPOSURE_AT_SHUTDOWN,
    FACT_TYPE_PAIRED_BINARY_SHUTDOWN_FORCE_FLATTEN_STARTED,
    FACT_TYPE_STRATEGY_LIFECYCLE_PRE_CLOSE_FLATTEN_REQUIRED,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
from tyrex_pm.strategies.paired_binary.exit_engine import ensure_pnl_budgets
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase
from paired_binary_shutdown_helpers import (
    YES,
    NO,
    app_cfg,
    coord_with_books,
    facts_from_sink,
    only_no_state,
    risk_cfg,
    seed_no_leg,
)


def _lifecycle_app_cfg(**pb_over):
    from tyrex_pm.runtime.config import parse_app_config

    pb = {
        "owner_id": "paired_binary",
        "market_id": "m1",
        "yes_token_id": YES,
        "no_token_id": NO,
        "position_size": "5",
        "max_pair_entry_cost": "1.02",
        "max_spread_yes": "0.05",
        "max_spread_no": "0.05",
        "pair_stop_loss_pct": "0.04",
        "pair_take_profit_pct": "0.10",
        "slippage_buffer": "0.005",
        "reject_if_spread_exceeds_loss_budget": False,
        "max_holding_time_s": 3600,
        "use_fixture_book": True,
        "fixture_yes_bid": "0.48",
        "fixture_yes_ask": "0.49",
        "fixture_no_bid": "0.50",
        "fixture_no_ask": "0.51",
        "tick_interval_s": 0.005,
        "max_runtime_s": 0.01,
        "exit_order_style": "FAK",
    }
    pb.update(pb_over)
    now = datetime.now(timezone.utc).timestamp()
    return parse_app_config(
        risk=risk_cfg(),
        strategy={"kind": "paired_binary", "enabled": True, "paired_binary": pb},
        runtime={
            "execution_mode": "shadow",
            "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
            "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
            "market_data": {"enabled": True, "max_book_age_s": 5},
            "execution": {"planner": {"enabled": True}},
            "observability": {"emit_decision_snapshot": True},
            "paired_binary": {
                "poll_interval_s": 0.005,
                "open_exposure_on_max_runtime": "force_reduce_only_exit",
                "open_exposure_timeout_s": 0.05,
            },
            "strategy_lifecycle": {
                "mode": "market_aware",
                "exit_clock_source": "event_end_ts",
                "max_runtime_s": None,
                "fallback_max_runtime_s": 900,
                "flatten_before_event_end_s": 20,
                "block_new_entry_phases": ["near_close", "closed"],
                "min_survival_window_s": 45,
            },
        },
    )


@pytest.mark.asyncio
async def test_survivor_not_force_flattened_by_old_max_runtime_when_clock_known(tmp_path: Path) -> None:
    """Ticks exceed strategy max_runtime_s but event_end_ts is far in the future."""
    now = datetime.now(timezone.utc).timestamp()
    app = _lifecycle_app_cfg(
        event_start_ts=now - 60,
        event_end_ts=now + 600,
        max_runtime_s=0.01,
    )
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    seed_no_leg(coord)
    state = only_no_state()
    ensure_pnl_budgets(state, cfg)
    stop = asyncio.Event()
    with JsonlSink(tmp_path / "facts.jsonl") as sink:

        async def _run() -> int:
            task = asyncio.create_task(
                run_paired_binary_loop(
                    app=app,
                    run_id=RunId("lifecycle-survivor-hold"),
                    coord=coord,
                    sink=sink,
                    oms=ShadowOMS(),
                    cfg=cfg,
                    state=state,
                    state_dir=tmp_path,
                    stop=stop,
                )
            )
            await asyncio.sleep(0.08)
            stop.set()
            return await task

        ticks = await _run()
        facts = facts_from_sink(sink)
    types = {f["fact_type"] for f in facts}
    assert ticks >= 2
    assert state.phase == PairedBinaryPhase.ONLY_NO_ACTIVE
    assert FACT_TYPE_PAIRED_BINARY_OPEN_EXPOSURE_AT_SHUTDOWN not in types


@pytest.mark.asyncio
async def test_pre_close_triggers_shutdown_flatten_path(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc).timestamp()
    app = _lifecycle_app_cfg(
        event_start_ts=now - 300,
        event_end_ts=now + 10,
        max_runtime_s=600,
    )
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    seed_no_leg(coord)
    state = only_no_state()
    ensure_pnl_budgets(state, cfg)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        await run_paired_binary_loop(
            app=app,
            run_id=RunId("lifecycle-pre-close"),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )
        facts = facts_from_sink(sink)
    types = {f["fact_type"] for f in facts}
    assert FACT_TYPE_STRATEGY_LIFECYCLE_PRE_CLOSE_FLATTEN_REQUIRED in types
    assert FACT_TYPE_PAIRED_BINARY_SHUTDOWN_FORCE_FLATTEN_STARTED in types


@pytest.mark.asyncio
async def test_unknown_clock_fallback_runtime_force_flatten_preserved(tmp_path: Path) -> None:
    """Without event_end_ts, short fallback_max_runtime still force-flattens open exposure."""
    from tyrex_pm.runtime.config import parse_app_config

    base = _lifecycle_app_cfg(event_start_ts=None, event_end_ts=None, max_runtime_s=600)
    app = parse_app_config(
        risk=risk_cfg(),
        strategy=base.raw["strategy"],
        runtime={
            **base.raw["runtime"],
            "strategy_lifecycle": {
                "mode": "market_aware",
                "fallback_max_runtime_s": 0.01,
                "flatten_before_event_end_s": 20,
            },
        },
    )
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    seed_no_leg(coord)
    state = only_no_state()
    ensure_pnl_budgets(state, cfg)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        await run_paired_binary_loop(
            app=app,
            run_id=RunId("lifecycle-fallback"),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )
        facts = facts_from_sink(sink)
    types = {f["fact_type"] for f in facts}
    assert "strategy_runtime_fallback_max_runtime" in types
    assert FACT_TYPE_PAIRED_BINARY_OPEN_EXPOSURE_AT_SHUTDOWN in types
