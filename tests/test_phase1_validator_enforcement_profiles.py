"""Phase 1 enforcement profile validator classifications."""

from __future__ import annotations

from scripts.validate_paired_binary_phase2_live_run import (
    PHASE1_ADVISORY_PASS,
    PHASE1_ENFORCE_SAFETY_FAIL,
    PHASE1_STALL_ENFORCE_PASS,
    PHASE1_SURVIVAL_PASS,
    PHASE1_TRAILING_ENFORCE_PASS,
    PHASE1_TRAILING_QUALITY_REJECT_PENDING_FAIL,
    _quality_reject_metrics,
    refine_phase1_classification,
)


def _rows(*fact_types: str) -> list[dict]:
    return [{"fact_type": ft, "payload": {}} for ft in fact_types]


def test_refine_advisory_pass() -> None:
    rows = _rows("survivor_reachability_scored")
    assert refine_phase1_classification(rows, PHASE1_SURVIVAL_PASS) == PHASE1_ADVISORY_PASS


def test_refine_trailing_enforce_pass() -> None:
    rows = _rows("survival_enforce_exit_submitted")
    rows[0]["payload"] = {"module": "trailing_stop", "trigger_type": "survival_trailing_stop"}
    assert refine_phase1_classification(rows, PHASE1_SURVIVAL_PASS) == PHASE1_TRAILING_ENFORCE_PASS


def test_refine_stall_downgrade_pass() -> None:
    rows = [
        {"fact_type": "survival_enforce_exit_requested", "payload": {"action": "downgrade", "module": "stall_exit"}},
        {"fact_type": "survivor_target_downgraded", "payload": {"from_mode": "full_recovery", "to_mode": "breakeven"}},
    ]
    assert refine_phase1_classification(rows, PHASE1_SURVIVAL_PASS) == PHASE1_STALL_ENFORCE_PASS


def test_enforce_safety_fail_duplicate_trailing() -> None:
    rows = [
        {"fact_type": "survival_enforce_exit_submitted", "payload": {"module": "trailing_stop"}},
        {"fact_type": "survival_enforce_exit_submitted", "payload": {"module": "trailing_stop"}},
    ]
    assert refine_phase1_classification(rows, PHASE1_SURVIVAL_PASS) == PHASE1_ENFORCE_SAFETY_FAIL


def test_trailing_quality_reject_retry_pass() -> None:
    rows = [
        {"fact_type": "survivor_trailing_stop_triggered", "payload": {}},
        {
            "fact_type": "survival_enforce_exit_skipped",
            "payload": {"skip_reason": "quality_reject", "retryable": True, "latched_intent": True},
        },
        {
            "fact_type": "survival_enforce_exit_retry_attempted",
            "payload": {"retry_outcome": "quality_passed"},
        },
        {"fact_type": "survival_enforce_exit_submitted", "payload": {"module": "trailing_stop"}},
        {"fact_type": "paired_binary_loop_stopped", "payload": {"final_state": "DONE"}},
    ]
    assert refine_phase1_classification(rows, PHASE1_SURVIVAL_PASS) == PHASE1_TRAILING_ENFORCE_PASS
    metrics = _quality_reject_metrics(rows)
    assert metrics["quality_reject_count"] == 1
    assert metrics["quality_reject_retry_count"] == 1


def test_trailing_quality_reject_pending_fail() -> None:
    rows = [
        {"fact_type": "survivor_trailing_stop_triggered", "payload": {}},
        {
            "fact_type": "survival_enforce_exit_skipped",
            "payload": {"skip_reason": "quality_reject", "quality_reject_detail": {"failed_sequence_gap": True}},
        },
        {
            "fact_type": "survival_enforce_exit_abandoned",
            "payload": {"reason": "pre_close_flatten_preempted"},
        },
        {"fact_type": "paired_binary_loop_stopped", "payload": {"final_state": "DONE"}},
    ]
    assert (
        refine_phase1_classification(rows, PHASE1_SURVIVAL_PASS)
        == PHASE1_TRAILING_QUALITY_REJECT_PENDING_FAIL
    )
