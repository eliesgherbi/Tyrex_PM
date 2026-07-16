"""Tests for M9 metric extraction and before/after comparison scripts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EXTRACT = REPO / "scripts" / "extract_paired_binary_metrics.py"
COMPARE = REPO / "scripts" / "compare_phase2_before_after.py"


@pytest.fixture
def minimal_run_dir(tmp_path: Path) -> Path:
    facts = tmp_path / "facts.jsonl"
    rows = [
        {
            "fact_type": "paired_binary_book_capture_quality",
            "payload": {
                "event": "activation",
                "yes_book_age_ms": 100,
                "no_book_age_ms": 120,
                "activation_book_age_ms": 120,
            },
        },
        {
            "fact_type": "decision_snapshot",
            "payload": {
                "decision_type": "entry_eval",
                "quality_report": {
                    "book_age_ms": 50,
                    "source": "websocket",
                    "verdict": "pass",
                    "source_quality": "ws_primary",
                },
            },
        },
        {
            "fact_type": "ws_primary_cutover",
            "payload": {},
        },
        {
            "fact_type": "execution_planner_evidence",
            "payload": {
                "worst_price_to_fill": 0.5,
                "sweep_vwap": 0.49,
                "available_depth": 10,
                "snapshot_id": "snap-1",
                "decision_id": "dec-1",
            },
        },
        {
            "fact_type": "latency_chain",
            "payload": {
                "trigger_to_submit_ms": 100,
                "submit_to_ack_ms": 50,
                "trigger_to_fill_ms": 150,
            },
        },
        {
            "fact_type": "paired_binary_done",
            "payload": {"state": "DONE"},
        },
    ]
    facts.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"run_name": "fixture_run", "run_id": "test-run-id", "git_sha": "abc123"}),
        encoding="utf-8",
    )
    return tmp_path


def test_extract_tolerates_missing_fields(minimal_run_dir: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(EXTRACT), str(minimal_run_dir)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    assert proc.returncode == 0, proc.stderr
    metrics = json.loads(proc.stdout)
    assert metrics["backbone"] == "WS_PRIMARY"
    assert metrics["activation_book_age_ms"]["p95"] == 120.0
    assert metrics["latency_ms"]["submit_to_ack_ms"]["non_null_count"] == 1
    assert metrics["planner_evidence_count"] == 1
    assert metrics["planner_evidence_incomplete"] == 0


def test_extract_skips_corrupt_jsonl_lines(tmp_path: Path) -> None:
    facts = tmp_path / "facts.jsonl"
    facts.write_text('{"fact_type":"paired_binary_done","payload":{"state":"DONE"}}\nNOT JSON\n', encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(EXTRACT), str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    assert proc.returncode == 0
    metrics = json.loads(proc.stdout)
    assert metrics["run_outcome"] == "DONE"


def test_compare_loads_reconstructed_controls(tmp_path: Path, minimal_run_dir: Path) -> None:
    out = tmp_path / "comparison.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(COMPARE),
            "--control",
            "paired_binary_live_1782741234",
            "--control",
            str(minimal_run_dir),
            "--treatment",
            str(minimal_run_dir),
            "--json-out",
            str(out),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(out.read_text(encoding="utf-8"))
    assert len(summary["control_runs"]) == 2
    assert summary["control_runs"][0]["provenance"] == "RECONSTRUCTED"
    assert "comparison_tables" in summary
