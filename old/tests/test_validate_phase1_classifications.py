"""Phase 1 validator classifications."""

from __future__ import annotations

from scripts.validate_paired_binary_phase2_live_run import (
    PHASE1_RUNTIME_PREMATURE_EXIT,
    PHASE1_TARGET_POLICY_PASS,
    PHASE1_SURVIVAL_PASS,
    PHASE2_WS_LIFECYCLE_PASS,
    PHASE2_WS_OPEN_EXPOSURE_FORCE_FLATTEN_PASS,
    classify_phase1_run,
    classify_phase2_run,
)


def _row(fact_type: str, payload: dict | None = None, *, ts: float | None = None) -> dict:
    row = {"fact_type": fact_type, "payload": payload or {}}
    if ts is not None:
        row["ts"] = ts
    return row


def _phase1_survival_rows() -> list[dict]:
    return [
        _row("survivor_target_selected", {"selected_target": "0.65", "enforcement_mode": "advisory"}),
        _row("survivor_reachability_scored", {"reachability_verdict": "reachable", "current_executable_bid": "0.60"}),
        _row("survivor_executable_exit_evaluated", {"snapshot_id": "snap1", "current_executable_bid": "0.60"}),
        _row("strategy_runtime_decision", {"continue_loop": True, "event_end_ts": 2_000_000.0}),
        _row("health", {"event": "paired_binary_loop_stopped", "final_state": "DONE"}),
    ]


def test_phase1_survival_pass_from_synthetic_facts() -> None:
    manifest = {"runtime": {"survival": {"enabled": True}}}
    result = classify_phase1_run(_phase1_survival_rows(), manifest=manifest)
    assert result["phase1_classification"] == PHASE1_TARGET_POLICY_PASS
    assert result["phase1_base_classification"] == PHASE1_SURVIVAL_PASS
    assert result["phase1_survival_enabled"] is True


def test_phase1_premature_runtime_exit() -> None:
    rows = [
        _row("strategy_runtime_decision", {"event_end_ts": 2_000_000.0}),
        _row("survivor_reachability_scored", {"reachability_verdict": "weak"}),
        _row(
            "paired_binary_open_exposure_at_shutdown",
            {"reason": "max_runtime_open_exposure"},
            ts=1_999_900.0,
        ),
        _row("health", {"event": "paired_binary_loop_stopped", "final_state": "DONE"}),
    ]
    manifest = {"runtime": {"survival": {"enabled": True}}}
    result = classify_phase1_run(rows, manifest=manifest)
    assert result["phase1_classification"] == PHASE1_RUNTIME_PREMATURE_EXIT
    assert result["runtime_premature_exit_detected"] is True


def _force_flatten_rows() -> list[dict]:
    return [
        _row("paired_binary_open_exposure_at_shutdown", {"reason": "max_runtime_open_exposure"}),
        _row("paired_binary_shutdown_force_flatten_started", {"reason": "max_runtime_open_exposure"}),
        _row("paired_binary_done", {"state": "DONE", "completion_reason": "shutdown_force_flatten"}),
        _row("paired_binary_shutdown_force_flatten_done", {"yes_qty": "0", "no_qty": "0"}),
        _row("decision_snapshot", {"decision_type": "urgent_exit", "decision_id": "d1"}),
        _row("execution_planner_evidence", {"decision_id": "d1"}),
        _row("oms_submit", {"side": "SELL", "source": "websocket"}),
        _row("latency_chain", {"decision_id": "d1"}),
        _row("paired_binary_realized_pnl", {"pnl_total": "0.10", "state": "DONE"}),
        _row("health", {"event": "paired_binary_loop_stopped", "final_state": "DONE", "ticks": 10}),
    ]


def test_phase2_classification_unchanged_when_survival_disabled() -> None:

    rows = _force_flatten_rows()
    phase1 = classify_phase1_run(rows, manifest={})
    assert phase1["phase1_survival_enabled"] is False
    assert phase1["phase1_classification"] is None
    phase2 = classify_phase2_run(rows)
    assert phase2["classification"] == PHASE2_WS_OPEN_EXPOSURE_FORCE_FLATTEN_PASS


def test_phase2_lifecycle_pass_unchanged_without_survival() -> None:
    rows = [
        _row("paired_binary_monitor_started", {}),
        _row("paired_binary_leg_stop", {"leg": "yes"}),
        _row("paired_binary_done", {"state": "DONE"}),
        _row("paired_binary_realized_pnl", {"pnl_total": "0.05"}),
        _row("health", {"event": "paired_binary_loop_stopped", "final_state": "DONE"}),
    ]
    phase1 = classify_phase1_run(rows)
    assert phase1["phase1_classification"] is None
    phase2 = classify_phase2_run(rows)
    assert phase2["classification"] == PHASE2_WS_LIFECYCLE_PASS
