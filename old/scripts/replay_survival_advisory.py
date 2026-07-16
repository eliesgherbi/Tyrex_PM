#!/usr/bin/env python3
"""Offline replay of survival advisory logic from facts.jsonl (Phase 1 M7)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SURVIVAL_FACT_TYPES = {
    "survivor_target_selected",
    "survivor_target_downgraded",
    "survivor_target_unreachable",
    "survivor_target_impossible",
    "survivor_executable_exit_evaluated",
    "survivor_reachability_scored",
    "survivor_progress_evaluated",
    "survivor_stall_detected",
    "survivor_trailing_stop_armed",
    "survivor_trailing_stop_triggered",
    "survivor_economics_evaluated",
    "survivor_early_exit_triggered",
    "kill_switch_triggered",
    "strategy_runtime_decision",
    "strategy_runtime_fallback_max_runtime",
    "strategy_lifecycle_entry_blocked",
    "strategy_lifecycle_pre_close_flatten_required",
}

PREMATURE_FLATTEN_MARKERS = ("max_runtime", "fallback_max_runtime", "tick_budget")


def _load_facts(path: Path) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        facts.append(json.loads(line))
    return facts


def _extract_survivor_events(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in facts if f.get("fact_type") in SURVIVAL_FACT_TYPES]


def _event_end_ts(facts: list[dict[str, Any]]) -> float | None:
    for row in reversed(facts):
        if row.get("fact_type") not in {"paired_binary_market_timing", "strategy_runtime_decision"}:
            continue
        raw = (row.get("payload") or {}).get("event_end_ts")
        if raw not in (None, ""):
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
    return None


def _detect_advisory_before_max_runtime(facts: list[dict[str, Any]]) -> dict[str, Any]:
    """Heuristic: advisory modules emitted before a max_runtime-style flatten."""
    advisory_ts: list[float] = []
    flatten_ts: list[float] = []
    for row in facts:
        ft = row.get("fact_type")
        ts_raw = row.get("ts")
        if ts_raw in (None, ""):
            continue
        try:
            ts = float(ts_raw)
        except (TypeError, ValueError):
            continue
        if ft in {
            "survivor_stall_detected",
            "survivor_trailing_stop_triggered",
            "survivor_economics_evaluated",
            "survivor_reachability_scored",
        }:
            advisory_ts.append(ts)
        if ft == "paired_binary_open_exposure_at_shutdown":
            reason = str((row.get("payload") or {}).get("reason") or "").lower()
            if any(m in reason for m in PREMATURE_FLATTEN_MARKERS):
                flatten_ts.append(ts)
    would_warn = bool(advisory_ts) and bool(flatten_ts) and min(advisory_ts) < min(flatten_ts)
    return {
        "advisory_events_before_flatten": would_warn,
        "first_advisory_ts": min(advisory_ts) if advisory_ts else None,
        "first_max_runtime_flatten_ts": min(flatten_ts) if flatten_ts else None,
        "event_end_ts": _event_end_ts(facts),
    }


def build_advisory_summary(facts: list[dict[str, Any]]) -> dict[str, Any]:
    events = _extract_survivor_events(facts)
    try:
        from tyrex_pm.strategies.paired_binary.terminal_reporting import (
            activation_unwind_failure_pattern,
            survivor_phase_reached,
        )

        reached = survivor_phase_reached(facts)
        pre_survivor_fail = activation_unwind_failure_pattern(facts)
    except ImportError:
        reached = False
        pre_survivor_fail = False

    summary: dict[str, Any] = {
        "fact_count": len(facts),
        "survival_event_count": len(events),
        "phase1_survival_enabled": len(events) > 0,
        "survivor_phase_reached": reached,
        "would_have_advised": [],
        "missing_fields": [],
        "unsupported_replay_reason": None,
        "by_type": {},
        "advisory_highlights": {},
        "kill_switch_events": [],
        "reachability_verdicts": [],
        "runtime_premature_exit_detected": False,
    }
    if pre_survivor_fail or not reached:
        summary["survival_advisory_not_exercised"] = True
        summary["survival_advisory_not_exercised_reason"] = (
            "activation_failed_before_survivor"
            if pre_survivor_fail
            else "survivor_phase_not_reached"
        )
        summary["survival_advisory_applicable"] = False
        summary["unsupported_replay_reason"] = "not_applicable_pre_survivor_failure"
    elif not events:
        summary["unsupported_replay_reason"] = "no_phase1_survival_facts"

    reachability_verdicts: set[str] = set()
    for ev in events:
        ft = ev.get("fact_type", "unknown")
        summary["by_type"][ft] = summary["by_type"].get(ft, 0) + 1
        payload = ev.get("payload") or {}
        entry = {
            "fact_type": ft,
            "ts": ev.get("ts"),
            "reachability_verdict": payload.get("reachability_verdict"),
            "economics_verdict": payload.get("economics_verdict"),
            "progress_ratio": payload.get("progress_ratio") or payload.get("progress_to_target"),
            "executable_bid": payload.get("executable_bid")
            or payload.get("current_executable_bid"),
            "enforcement_mode": payload.get("enforcement_mode"),
            "selected_target": payload.get("selected_target") or payload.get("target_price"),
            "switch_name": payload.get("switch_name"),
            "action": payload.get("action"),
        }
        if entry["reachability_verdict"]:
            reachability_verdicts.add(str(entry["reachability_verdict"]))
        if ft == "survivor_reachability_scored" and not entry["executable_bid"]:
            summary["missing_fields"].append({"fact_type": ft, "missing": "current_executable_bid"})
        if ft == "kill_switch_triggered":
            summary["kill_switch_events"].append(
                {
                    "switch_name": payload.get("switch_name"),
                    "action": payload.get("action"),
                    "reason": payload.get("reason"),
                }
            )
        summary["would_have_advised"].append(entry)

    timing = _detect_advisory_before_max_runtime(facts)
    summary["advisory_highlights"] = {
        "stall_events": summary["by_type"].get("survivor_stall_detected", 0),
        "trailing_armed": summary["by_type"].get("survivor_trailing_stop_armed", 0),
        "trailing_triggers": summary["by_type"].get("survivor_trailing_stop_triggered", 0),
        "reachability_scores": summary["by_type"].get("survivor_reachability_scored", 0),
        "economics_evaluations": summary["by_type"].get("survivor_economics_evaluated", 0),
        "economics_negative": sum(
            1
            for e in summary["would_have_advised"]
            if e.get("economics_verdict") in {"exit_survivor_early", "negative", "reject"}
        ),
        "kill_switch_triggered": summary["by_type"].get("kill_switch_triggered", 0),
        **timing,
    }
    summary["reachability_verdicts"] = sorted(reachability_verdicts)
    summary["stall_detected"] = summary["by_type"].get("survivor_stall_detected", 0) > 0
    summary["trailing_armed"] = summary["by_type"].get("survivor_trailing_stop_armed", 0) > 0
    summary["trailing_triggered"] = summary["by_type"].get("survivor_trailing_stop_triggered", 0) > 0
    summary["economics_negative"] = summary["advisory_highlights"]["economics_negative"] > 0
    summary["kill_switch_triggered"] = summary["advisory_highlights"]["kill_switch_triggered"] > 0
    summary["runtime_premature_exit_detected"] = timing.get("first_max_runtime_flatten_ts") is not None and (
        timing.get("event_end_ts") is not None
        and timing["first_max_runtime_flatten_ts"] < timing["event_end_ts"] - 20
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay survival advisory summary from facts.jsonl")
    parser.add_argument("facts_path", type=Path, help="Path to facts.jsonl")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Write JSON summary to this path (default: stdout)",
    )
    args = parser.parse_args()
    facts = _load_facts(args.facts_path)
    summary = build_advisory_summary(facts)
    text = json.dumps(summary, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
