"""Validator classification for activation/unwind failure runs."""

from __future__ import annotations

from scripts.validate_paired_binary_phase2_live_run import (
    PHASE1_SURVIVAL_PASS,
    PHASE2_WS_ACTIVATION_UNWIND_FAIL,
    PHASE2_WS_FAIL,
    classify_phase1_run,
    classify_phase2_run,
)


def _row(fact_type: str, payload: dict | None = None) -> dict:
    return {"fact_type": fact_type, "payload": payload or {}}


def _activation_fail_rows(*, with_terminal: bool = True, with_pnl: bool = True) -> list[dict]:
    rows = [
        _row(
            "paired_binary_market_timing",
            {
                "market_id": "btc_5m_20260701_2110",
                "event_start_ts": 1782939900.0,
                "event_end_ts": 1782940200.0,
                "phase": "active",
            },
        ),
        _row("paired_binary_pair_entry_committed"),
        _row("paired_binary_state_change", {"state": "BOTH_LEGS_FILLED", "to": "BOTH_LEGS_FILLED"}),
        _row("paired_binary_activation_rejected_loss_budget", {"reason": "no_activation_gap_exceeds_loss_budget"}),
        _row("paired_binary_emergency_unwind_started"),
        _row("paired_binary_emergency_unwind_attempt"),
        _row("paired_binary_emergency_unwind_done", {"attempt_count": 2}),
        _row("strategy_terminal_safe_to_stop", {"final_state": "FAILED"}),
        _row("health", {"event": "paired_binary_loop_stopped", "final_state": "FAILED", "ticks": 31}),
    ]
    if with_terminal:
        rows.append(
            _row(
                "paired_binary_terminal_summary",
                {
                    "final_state": "FAILED",
                    "terminal_reason": "no_activation_gap_exceeds_loss_budget",
                    "survivor_phase_reached": False,
                    "emergency_unwind_attempted": True,
                    "pnl_unavailable_reason": "activation_unwind_failed_or_incomplete",
                },
            )
        )
    if with_pnl:
        rows.append(
            _row(
                "paired_binary_realized_pnl_unavailable",
                {"reason": "activation_unwind_failed_or_incomplete", "final_state": "FAILED"},
            )
        )
    return rows


def test_classifies_activation_unwind_fail_with_diagnostics() -> None:
    result = classify_phase2_run(_activation_fail_rows())
    assert result["classification"] == PHASE2_WS_ACTIVATION_UNWIND_FAIL
    assert result["missing_requirements"] == []
    assert result["survivor_phase_reached"] is False
    assert result["is_phase1_advisory_pass"] is False
    assert result["hardening_metadata_ok"] is True


def test_activation_unwind_fail_missing_terminal_summary() -> None:
    result = classify_phase2_run(_activation_fail_rows(with_terminal=False))
    assert result["classification"] == PHASE2_WS_ACTIVATION_UNWIND_FAIL
    assert "missing_fact:paired_binary_terminal_summary" in result["missing_requirements"]


def test_activation_unwind_fail_does_not_require_paired_binary_done() -> None:
    result = classify_phase2_run(_activation_fail_rows())
    assert "missing_fact:paired_binary_done" not in result["missing_requirements"]


def test_phase1_not_survival_pass_for_activation_fail() -> None:
    manifest = {"runtime": {"survival": {"enabled": True}}}
    result = classify_phase1_run(_activation_fail_rows(), manifest=manifest)
    assert result["phase1_classification"] is None
    assert result["is_phase1_advisory_pass"] is False
    assert result["survival_advisory_not_exercised"] is True
    assert result["survival_advisory_not_exercised_reason"] == "activation_failed_before_survivor"


def test_done_run_still_requires_paired_binary_done() -> None:
    rows = [
        _row("paired_binary_monitor_started"),
        _row("paired_binary_state_change", {"state": "BOTH_LEGS_ACTIVE"}),
        _row("paired_binary_leg_stop"),
        _row("health", {"event": "paired_binary_loop_stopped", "final_state": "DONE"}),
    ]
    result = classify_phase2_run(rows)
    assert "missing_fact:paired_binary_done" in result["normal_lifecycle_missing"]


def test_phase1_survival_pass_unchanged() -> None:
    rows = [
        _row("survivor_target_selected", {"selected_target": "0.65"}),
        _row("survivor_reachability_scored", {"reachability_verdict": "reachable"}),
        _row("paired_binary_monitor_started"),
        _row("paired_binary_state_change", {"state": "ONLY_YES_ACTIVE"}),
        _row("health", {"event": "paired_binary_loop_stopped", "final_state": "DONE"}),
    ]
    manifest = {"runtime": {"survival": {"enabled": True}}}
    result = classify_phase1_run(rows, manifest=manifest)
    assert result["phase1_classification"] == PHASE1_SURVIVAL_PASS
