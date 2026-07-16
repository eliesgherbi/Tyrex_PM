"""Unit tests for scripts/validate_m8_ws_primary_run.py."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.validate_m8_ws_primary_run import analyze


def test_analyze_detects_rest_oms_submit_violation(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    rows = [
        {
            "fact_type": "oms_submit",
            "payload": {"book_source": "rest_bootstrap", "side": "BUY"},
        },
        {"fact_type": "rest_poll_disabled", "payload": {}},
    ]
    (run / "facts.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    summary = analyze(run)
    crit4 = next(c for c in summary["acceptance_criteria"] if c["id"] == 4)
    assert crit4["status"] == "FAIL"


def test_analyze_ignores_rest_entry_eval_observability_only(tmp_path: Path) -> None:
    run = tmp_path / "run2"
    run.mkdir()
    rows = [
        {
            "fact_type": "decision_snapshot",
            "payload": {
                "decision_id": "d1",
                "decision_type": "entry_eval",
                "snapshot_ids": {"y": "s1"},
                "quality_report": {
                    "verdict": "reject_decision",
                    "book_age_ms": 100,
                    "source": "rest_bootstrap",
                },
            },
        },
        {"fact_type": "rest_poll_disabled", "payload": {}},
    ]
    (run / "facts.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    summary = analyze(run)
    assert not summary["rest_sourced_entries"]
    crit4 = next(c for c in summary["acceptance_criteria"] if c["id"] == 4)
    assert crit4["status"] == "PASS"


def test_analyze_passes_ws_primary_snapshot(tmp_path: Path) -> None:
    run = tmp_path / "run2"
    run.mkdir()
    rows = [
        {
            "fact_type": "decision_snapshot",
            "payload": {
                "decision_id": "d2",
                "decision_type": "stop_trigger",
                "snapshot_ids": {"y": "s2", "n": "s3"},
                "quality_report": {
                    "verdict": "pass",
                    "book_age_ms": 200,
                    "source": "websocket",
                },
            },
        },
        {
            "fact_type": "execution_planner_evidence",
            "payload": {
                "decision_id": "d2",
                "snapshot_id": "s2",
                "worst_price_to_fill": "0.45",
                "sweep_vwap": "0.45",
                "available_depth": "100",
            },
        },
        {"fact_type": "rest_poll_disabled", "payload": {}},
        {"fact_type": "paired_binary_tick_source", "payload": {"tick_source": "event_wake"}},
        {"fact_type": "paired_binary_tick_source", "payload": {"tick_source": "timer"}},
    ]
    (run / "facts.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    summary = analyze(run)
    crit5 = next(c for c in summary["acceptance_criteria"] if c["id"] == 5)
    assert crit5["status"] == "PASS"
    assert summary["book_age_ms"]["p95"] == 200.0
