"""Phase 2 validator classifications for shutdown force-flatten runs."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.validate_paired_binary_phase2_live_run import (
    PHASE2_WS_FAIL,
    PHASE2_WS_LIFECYCLE_PASS,
    PHASE2_WS_OPEN_EXPOSURE_FORCE_FLATTEN_PASS,
    analyze_phase2,
    classify_phase2_run,
)


def _row(fact_type: str, payload: dict | None = None) -> dict:
    return {"fact_type": fact_type, "payload": payload or {}}


def _force_flatten_rows(*, include_done: bool = True, include_pnl: bool = True) -> list[dict]:
    rows = [
        _row("paired_binary_open_exposure_at_shutdown", {"reason": "max_runtime_open_exposure"}),
        _row("paired_binary_shutdown_force_flatten_started", {"reason": "max_runtime_open_exposure"}),
        _row(
            "paired_binary_shutdown_force_flatten_done",
            {"yes_qty": "0", "no_qty": "0", "reason": "max_runtime_open_exposure"},
        ),
        _row("decision_snapshot", {"decision_type": "urgent_exit", "decision_id": "d1"}),
        _row("execution_planner_evidence", {"decision_id": "d1"}),
        _row("oms_submit", {"side": "SELL", "source": "websocket"}),
        _row("latency_chain", {"decision_id": "d1"}),
        _row("health", {"event": "paired_binary_loop_stopped", "final_state": "DONE", "ticks": 10}),
    ]
    if include_done:
        rows.insert(
            3,
            _row(
                "paired_binary_done",
                {"state": "DONE", "completion_reason": "shutdown_force_flatten"},
            ),
        )
    if include_pnl:
        rows.insert(
            4,
            _row("paired_binary_realized_pnl", {"pnl_total": "0.10", "state": "DONE"}),
        )
    elif include_done:
        rows.insert(
            4,
            _row(
                "paired_binary_realized_pnl_unavailable",
                {"reason": "missing_exit_cashflow", "missing_fields": ["yes_exit_cash"]},
            ),
        )
    return rows


def test_validator_classifies_force_flatten_pass() -> None:
    result = classify_phase2_run(_force_flatten_rows())
    assert result["classification"] == PHASE2_WS_OPEN_EXPOSURE_FORCE_FLATTEN_PASS
    assert result["lifecycle_complete"] is True
    assert result["force_flatten_complete"] is True
    assert result["pnl_computed"] is True


def test_validator_force_flatten_requires_done_fact() -> None:
    result = classify_phase2_run(_force_flatten_rows(include_done=False))
    assert result["classification"] == PHASE2_WS_FAIL
    assert "missing_fact:paired_binary_done" in result["force_flatten_missing"]


def test_validator_force_flatten_failure_is_fail() -> None:
    rows = _force_flatten_rows()
    rows.append(
        _row(
            "paired_binary_shutdown_force_flatten_failed",
            {"failure_reason": "residual_exposure"},
        )
    )
    result = classify_phase2_run(rows)
    assert result["classification"] == PHASE2_WS_FAIL


def test_validator_normal_lifecycle_pass() -> None:
    rows = [
        _row("paired_binary_monitor_started", {"state": "BOTH_LEGS_ACTIVE"}),
        _row("paired_binary_leg_stop", {"leg": "yes"}),
        _row("paired_binary_winner_target", {"leg": "no"}),
        _row("paired_binary_done", {"state": "DONE"}),
        _row("paired_binary_realized_pnl", {"pnl_total": "0.90"}),
        _row("health", {"event": "paired_binary_loop_stopped", "final_state": "DONE"}),
    ]
    result = classify_phase2_run(rows)
    assert result["classification"] == PHASE2_WS_LIFECYCLE_PASS
    assert result["lifecycle_complete"] is True


def test_analyze_phase2_on_live3_reports_legacy_gap() -> None:
    live3 = Path(__file__).resolve().parents[1] / "var/reporting/runs/paired_binary_ws_primary_live3_1782903844"
    if not (live3 / "facts.jsonl").is_file():
        return
    summary = analyze_phase2(live3)
    fact_types = {
        json.loads(ln)["fact_type"]
        for ln in (live3 / "facts.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    }
    if "paired_binary_done" not in fact_types:
        assert summary["classification"] != PHASE2_WS_OPEN_EXPOSURE_FORCE_FLATTEN_PASS
        assert "missing_fact:paired_binary_done" in summary["force_flatten_missing"]
