from __future__ import annotations

import json
from pathlib import Path

from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.reporting.summarize import summarize_run


def test_jsonl_and_summarize(tmp_path: Path) -> None:
    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    with JsonlSink(run_dir / "facts.jsonl") as sink:
        sink.write(make_fact("risk_decision", "run1", {"reason_codes": ["kill_switch"]}))
        sink.write(make_fact("guru_signal", "run1", {"dedup": "a"}))
    s = summarize_run(run_dir)
    assert s["facts"] == 2
    assert s["by_type"]["risk_decision"] == 1
    assert "join_audit" in s
