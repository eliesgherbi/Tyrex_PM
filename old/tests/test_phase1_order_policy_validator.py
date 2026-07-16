"""Validator order-policy safety classification tests."""

from __future__ import annotations

from scripts.validate_paired_binary_phase2_live_run import (
    PHASE1_ORDER_POLICY_SAFETY_FAIL,
    PHASE1_SURVIVAL_PASS,
    _order_policy_report,
    _order_policy_safety_fail,
    refine_phase1_classification,
)


def _row(fact_type: str, payload: dict | None = None, *, ts: float = 1.0) -> dict:
    return {"fact_type": fact_type, "payload": payload or {}, "ts": ts}


def test_validator_flags_stale_resting_order() -> None:
    rows = [
        _row("survival_exit_resting_order_placed", {"order_id": "a", "time_to_close": 120}, ts=10),
        _row("paired_binary_loop_stopped", {"final_state": "DONE"}, ts=20),
    ]
    fail, violations = _order_policy_safety_fail(rows)
    assert fail is True
    assert "gtc_gtd_left_open_after_terminal" in violations
    refined = refine_phase1_classification(rows, PHASE1_SURVIVAL_PASS)
    assert refined == PHASE1_ORDER_POLICY_SAFETY_FAIL


def test_validator_reports_order_policy_metrics() -> None:
    rows = [
        _row(
            "survival_exit_order_type_selected",
            {"policy_mode": "fak_retry", "order_type": "FAK"},
        ),
        _row("survival_exit_order_repriced", {"order_type": "FAK"}),
        _row(
            "survival_exit_resting_order_placed",
            {"order_id": "x", "time_to_close": 120},
        ),
        _row("survival_exit_resting_order_cancelled", {"order_id": "x"}),
        _row("paired_binary_loop_stopped", {"final_state": "DONE"}, ts=99),
    ]
    metrics = _order_policy_report(rows)
    assert metrics["survival_exit_order_policy_mode"] == "fak_retry"
    assert metrics["fak_retry_count"] == 1
    assert metrics["managed_rest_used"] is True
    assert metrics["resting_order_cancelled"] is True
    assert metrics["stale_order_left_open"] is False


def test_validator_flags_post_only_survival() -> None:
    rows = [
        _row("survival_exit_order_type_selected", {"post_only": True}),
    ]
    fail, violations = _order_policy_safety_fail(rows)
    assert fail is True
    assert "post_only_survival_exit" in violations
