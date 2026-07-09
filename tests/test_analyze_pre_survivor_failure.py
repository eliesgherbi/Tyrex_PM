"""Analysis helpers for pre-survivor failure runs."""

from __future__ import annotations

from scripts.analyze_live_runs_review import _hardening_section, _survival_advisory_section
from scripts.replay_survival_advisory import build_advisory_summary


def _rows() -> list[dict]:
    return [
        {
            "fact_type": "paired_binary_market_timing",
            "payload": {
                "market_id": "btc_5m_20260701_2110",
                "event_start_ts": 1.0,
                "event_end_ts": 2.0,
                "phase": "active",
            },
        },
        {"fact_type": "paired_binary_pair_entry_committed", "payload": {}},
        {"fact_type": "paired_binary_state_change", "payload": {"state": "BOTH_LEGS_FILLED"}},
        {"fact_type": "paired_binary_emergency_unwind_started", "payload": {}},
        {"fact_type": "paired_binary_emergency_unwind_attempt", "payload": {}},
        {
            "fact_type": "health",
            "payload": {"event": "paired_binary_loop_stopped", "final_state": "FAILED"},
        },
    ]


def test_survival_advisory_section_marks_not_applicable() -> None:
    section = _survival_advisory_section(_rows())
    assert section["survival_advisory_not_exercised"] is True
    assert section["survival_advisory_not_exercised_reason"] == "activation_failed_before_survivor"
    assert section["survivor_phase_reached"] is False


def test_replay_marks_pre_survivor_failure() -> None:
    summary = build_advisory_summary(_rows())
    assert summary["survival_advisory_not_exercised"] is True
    assert summary["unsupported_replay_reason"] == "not_applicable_pre_survivor_failure"


def test_hardening_section_reads_market_timing() -> None:
    section = _hardening_section(_rows())
    assert section["hardening_metadata_ok"] is True
    assert section["lifecycle_clock_known"] is True
    assert section["final_state"] == "FAILED"
