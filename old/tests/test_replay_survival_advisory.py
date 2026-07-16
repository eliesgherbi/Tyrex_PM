#!/usr/bin/env python3
"""Replay survival advisory tests (Phase 1 M7)."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.replay_survival_advisory import build_advisory_summary


def test_replay_summary_from_facts(tmp_path: Path) -> None:
    facts_path = tmp_path / "facts.jsonl"
    facts_path.write_text(
        json.dumps(
            {
                "fact_type": "survivor_reachability_scored",
                "payload": {
                    "reachability_verdict": "weak",
                    "current_executable_bid": "0.55",
                    "enforcement_mode": "advisory",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    summary = build_advisory_summary([json.loads(facts_path.read_text())])
    assert summary["survival_event_count"] == 1
    assert summary["by_type"]["survivor_reachability_scored"] == 1


def test_replay_reports_missing_fields_and_unsupported() -> None:
    summary = build_advisory_summary([])
    assert summary["unsupported_replay_reason"] == "no_phase1_survival_facts"
    summary2 = build_advisory_summary(
        [
            {
                "fact_type": "survivor_reachability_scored",
                "payload": {"reachability_verdict": "weak"},
            }
        ]
    )
    assert any(m["missing"] == "current_executable_bid" for m in summary2["missing_fields"])


def test_replay_kill_switch_summary() -> None:
    summary = build_advisory_summary(
        [
            {
                "fact_type": "kill_switch_triggered",
                "payload": {
                    "switch_name": "daily_max_loss_usd",
                    "action": "deny_entry",
                    "reason": "kill_switch_daily_max_loss_usd",
                },
            }
        ]
    )
    assert summary["kill_switch_triggered"] is True
    assert summary["advisory_highlights"]["kill_switch_triggered"] == 1
