"""watch-run: read-only snapshot of a run's journal, windows and ledger."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from tyrex_pm.application.cli import main
from tyrex_pm.application.watch_run import snapshot
from tyrex_pm.persistence.run_evidence_journal import SqliteRunEvidenceJournal

ROOT = Path(__file__).resolve().parents[3]
PAPER = ROOT / "config" / "runs" / "settlement_value_continuous_paper.yaml"


def _config(tmp_path: Path) -> Path:
    raw = yaml.safe_load(PAPER.read_text(encoding="utf-8"))
    raw["state_directory"] = str(tmp_path / "paper" / "state")
    raw["report_directory"] = str(tmp_path / "paper" / "runs")
    raw["settlement"]["ledger_path"] = str(tmp_path / "paper" / "ledger.jsonl")
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_snapshot_shows_decisions_entries_and_windows(tmp_path: Path, capsys) -> None:
    cfg = _config(tmp_path)
    assert "no run journal yet" in snapshot(cfg)
    state = tmp_path / "paper" / "state"
    state.mkdir(parents=True)
    check = SqliteRunEvidenceJournal(state / "execution.sqlite3", run_instance_id="check")
    check.append("RUN_STARTED", {"live_requested": False})
    check.close()
    journal = SqliteRunEvidenceJournal(state / "execution.sqlite3", run_instance_id="live-1")
    journal.append("RUN_STARTED", {"live_requested": True, "venue": "paper",
                                   "run_mode": "continuous"})
    journal.append("STRATEGY_DECISION", {"action": "WAIT", "reason_code": "EDGE_BELOW_COST",
                                         "window_id": "w1", "decision_evidence": {}})
    journal.append("ENTRY_OUTCOME", {"window_id": "w1", "episode_seq": 1, "attempt_seq": 1,
                                     "kind": "FILLED", "confirmed_shares": "10"})
    journal.append("RECORDING_STATUS", {"level": "decisions", "capture_cost_us": {"p99": 41.0},
                                        "streams": {"decisions": {"written": 120,
                                                                  "dropped": 0}}})
    journal.close()
    late = SqliteRunEvidenceJournal(state / "execution.sqlite3", run_instance_id="check-2")
    late.append("RUN_STARTED", {"live_requested": False})
    late.close()
    run_dir = tmp_path / "paper" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "windows.jsonl").write_text(json.dumps({"window_id": "w1", "episodes": 1}) + "\n")
    text = snapshot(cfg)
    assert "run live-1" in text  # the trading run, not the later account check
    assert "WAIT / EDGE_BELOW_COST" in text and "FILLED" in text
    assert "windows completed: 1" in text
    assert "recording (decisions): decisions 120 written/0 dropped; capture p99 41.0us" in text
    assert main(["watch-run", "--config", str(cfg)]) == 0
    assert "live-1" in capsys.readouterr().out
