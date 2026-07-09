"""No-entry shutdown summary fact aggregation."""

from __future__ import annotations

import json

import pytest

from tyrex_pm.core.ids import RunId
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
from tyrex_pm.strategies.paired_binary.no_entry_summary import build_no_entry_summary, had_pair_entry
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from paired_binary_shutdown_helpers import app_cfg, coord_with_books, facts_from_sink


def _row(fact_type: str, payload: dict) -> dict:
    return {"fact_type": fact_type, "payload": payload}


def test_build_no_entry_summary_aggregates_skip_and_quality_reasons() -> None:
    rows = [
        _row("paired_binary_entry_eval", {"pair_cost": "1.01"}),
        _row("paired_binary_entry_skip", {"reason": "readiness_not_trading_enabled", "pair_cost": "1.02"}),
        _row("paired_binary_entry_skip", {"reason": "pair_cost_too_high", "pair_cost": "1.03"}),
        _row("paired_binary_entry_skip", {"reason": "readiness_not_trading_enabled"}),
        _row("data_quality_verdict", {"verdict": "reject", "reasons": ["source_not_ws_primary", "reconnect_gap"]}),
        _row("market_data_health_block", {"block_reason": "readiness_not_trading_enabled", "readiness_state": "blocked"}),
        _row("paired_binary_market_timing", {"phase": "active"}),
    ]
    summary = build_no_entry_summary(
        rows,
        run_id="run-1",
        market_id="m1",
        yes_token_id="yes",
        no_token_id="no",
        ticks=14,
        duration_s=12.5,
        final_state="IDLE",
        last_market_timing_phase="active",
    )
    assert summary is not None
    assert summary["entry_eval_count"] == 1
    assert summary["pair_entry_submit_count"] == 0
    assert summary["top_skip_reasons"]["readiness_not_trading_enabled"] == 2
    assert summary["top_skip_reasons"]["pair_cost_too_high"] == 1
    assert summary["quality_reject_reasons"]["source_not_ws_primary"] == 1
    assert summary["last_market_timing_phase"] == "active"
    assert summary["pair_cost_min"] == "1.01"
    assert summary["pair_cost_max"] == "1.03"


def test_had_pair_entry_true_when_committed() -> None:
    rows = [_row("paired_binary_pair_entry_committed", {})]
    assert had_pair_entry(rows, "IDLE") is True


def test_build_no_entry_summary_none_when_pair_entered() -> None:
    rows = [_row("paired_binary_pair_entry_committed", {})]
    assert (
        build_no_entry_summary(
            rows,
            run_id="run-1",
            market_id="m1",
            yes_token_id="yes",
            no_token_id="no",
            ticks=1,
            duration_s=1.0,
            final_state="BOTH_ENTRY_PENDING",
            last_market_timing_phase=None,
        )
        is None
    )


@pytest.mark.asyncio
async def test_dry_run_emits_no_entry_summary_at_shutdown(tmp_path) -> None:
    app = app_cfg(
        max_runtime_s=0.02,
        entry_dry_run=True,
        event_start_ts=1_000_000_000.0,
        event_end_ts=1_000_000_300.0,
    )
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    state = PairedBinaryRuntimeState(phase=PairedBinaryPhase.IDLE)

    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        await run_paired_binary_loop(
            app=app,
            run_id=RunId("no-entry-summary"),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )
        facts = facts_from_sink(sink)

    summaries = [f for f in facts if f.get("fact_type") == "paired_binary_no_entry_summary"]
    assert len(summaries) == 1
    payload = summaries[0]["payload"]
    assert payload["pair_entry_submit_count"] == 0
    assert payload["final_state"] == "IDLE"
    assert "top_skip_reasons" in payload
