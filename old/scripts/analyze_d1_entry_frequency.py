#!/usr/bin/env python3
"""D1 entry-frequency analysis for Z-Gap observe-only runs."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

INFRA_SKIP_REASONS = frozenset(
    {
        "z_gap_ptb_missing",
        "z_gap_ptb_unverified",
        "z_gap_ptb_late",
        "z_gap_ptb_mismatch",
        "z_gap_feed_stale",
        "z_gap_chainlink_stale_basis_untrusted",
        "z_gap_clock_sync_failed",
        "z_gap_clock_drift_exceeded",
        "z_gap_sigma_not_ready",
        "z_gap_fee_model_unknown",
        "z_gap_book_stale",
        "z_gap_quality_reject",
    }
)

MARKET_SKIP_REASONS = frozenset(
    {
        "z_gap_basis_exceeded_fresh_chainlink",
        "z_gap_edge_below_theta",
        "z_gap_tau_out_of_band",
        "z_gap_z_out_of_band",
        "z_gap_jump_guard",
    }
)

TINY_PARAMS = {
    "theta_take": "0.05",
    "theta_fill_floor": "0.03",
    "z_band": ["0.8", "2.2"],
    "tau_band_s": [60, 210],
    "basis_max_bps": "3",
    "sizing_max_usd": "5",
}


def _load_facts(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _discover_observe_runs(runs_dir: Path) -> list[Path]:
    if not runs_dir.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(runs_dir.iterdir()):
        if not child.is_dir():
            continue
        if not (child / "facts.jsonl").is_file():
            continue
        name = child.name.lower()
        if "z_gap" in name and "observe" in name:
            out.append(child)
    return out


def _gate_failures(gate_results: dict[str, str]) -> list[str]:
    return [g for g, v in gate_results.items() if str(v).lower() == "fail"]


def analyze_runs(run_dirs: list[Path]) -> dict[str, Any]:
    windows = 0
    eval_ticks = 0
    would_enter = 0
    candidate_windows = 0
    leg_dist: Counter[str] = Counter()
    tau_first: list[float] = []
    time_to_first_candidate_s: list[float] = []
    candidate_ticks_per_window: list[int] = []
    skip_hist: Counter[str] = Counter()
    gate_fail_hist: Counter[str] = Counter()
    infra_blocks = 0
    market_blocks = 0
    per_window: list[dict[str, Any]] = []

    for run_dir in run_dirs:
        facts = _load_facts(run_dir / "facts.jsonl")
        if not facts:
            continue
        windows += 1
        window_would = 0
        window_candidates = 0
        first_candidate_ts: str | None = None
        run_start_ts: str | None = facts[0].get("ts") if facts else None

        for row in facts:
            ft = row.get("fact_type")
            if ft not in {"z_gap_entry_eval", "z_gap_entry_skip"}:
                continue
            payload = row.get("payload") or {}
            eval_ticks += 1
            status = str(payload.get("decision_status") or "")
            reason = str(payload.get("reason_code") or "")
            if reason:
                skip_hist[reason] += 1
            gates = payload.get("gate_results") or {}
            if isinstance(gates, dict):
                for g in _gate_failures(gates):
                    gate_fail_hist[g] += 1
            if reason in INFRA_SKIP_REASONS:
                infra_blocks += 1
            elif reason in MARKET_SKIP_REASONS or status == "not_ready":
                market_blocks += 1
            if status == "would_enter":
                would_enter += 1
                window_would += 1
                window_candidates += 1
                leg = str(payload.get("selected_leg") or "unknown")
                leg_dist[leg] += 1
                tau = payload.get("tau_s")
                if tau is not None:
                    try:
                        tau_first.append(float(tau))
                    except (TypeError, ValueError):
                        pass
                if first_candidate_ts is None:
                    first_candidate_ts = str(row.get("ts") or "")
                    if run_start_ts and first_candidate_ts:
                        try:
                            t0 = datetime.fromisoformat(run_start_ts.replace("Z", "+00:00"))
                            t1 = datetime.fromisoformat(first_candidate_ts.replace("Z", "+00:00"))
                            time_to_first_candidate_s.append((t1 - t0).total_seconds())
                        except ValueError:
                            pass

        if window_would > 0:
            candidate_windows += 1
        candidate_ticks_per_window.append(window_candidates)
        terminal = [r for r in facts if r.get("fact_type") == "z_gap_terminal_summary"]
        terminal_payload = (terminal[-1].get("payload") or {}) if terminal else {}
        per_window.append(
            {
                "run_name": run_dir.name,
                "evaluation_count": terminal_payload.get("evaluation_count"),
                "would_enter_count": terminal_payload.get("would_enter_count", window_would),
                "operational_pass": terminal_payload.get("operational_pass"),
                "skip_reason_histogram": terminal_payload.get("skip_reason_histogram"),
            }
        )

    pct_candidate_windows = (candidate_windows / windows * 100.0) if windows else 0.0
    est_windows_until_candidate = None
    if candidate_windows > 0 and windows > 0:
        est_windows_until_candidate = round(windows / candidate_windows, 2)
    elif windows > 0:
        est_windows_until_candidate = None

    confidence = "low"
    if windows >= 12 and eval_ticks >= 100:
        confidence = "medium"
    if windows >= 30 and would_enter >= 3:
        confidence = "high"

    return {
        "analyzed_at_utc": datetime.now(timezone.utc).isoformat(),
        "parameters": TINY_PARAMS,
        "windows_complete": windows,
        "evaluation_ticks_analyzed": eval_ticks,
        "would_enter_count": would_enter,
        "windows_with_candidate": candidate_windows,
        "pct_windows_with_candidate": round(pct_candidate_windows, 3),
        "candidate_leg_distribution": dict(leg_dist),
        "first_candidate_tau_s_distribution": {
            "count": len(tau_first),
            "min": min(tau_first) if tau_first else None,
            "max": max(tau_first) if tau_first else None,
            "samples": tau_first[:20],
        },
        "time_to_first_candidate_s": {
            "count": len(time_to_first_candidate_s),
            "samples": time_to_first_candidate_s[:20],
        },
        "candidate_ticks_per_window": candidate_ticks_per_window,
        "skip_reason_histogram": dict(skip_hist),
        "dominant_blocking_gates": gate_fail_hist.most_common(10),
        "block_class_infra_or_model_unavailable": infra_blocks,
        "block_class_market_conditions": market_blocks,
        "estimated_windows_until_one_candidate": est_windows_until_candidate,
        "confidence_level": confidence,
        "limitations": [
            "Entry facts are deduped on (decision_status, reason_code, selected_leg, selected_edge); tick counts undercount total 1s evaluations.",
            "No profitability or statistical edge claim.",
            "Candidate frequency uses exact live_z_gap_tiny.yaml entry parameters (not retuned).",
        ],
        "per_window": per_window,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="D1 Z-Gap entry-frequency analysis")
    parser.add_argument("--runs-dir", default="var/reporting/runs")
    parser.add_argument("--runs", nargs="*", default=None, help="Explicit run dirs or names")
    parser.add_argument("--output-json", default="var/reporting/z_gap/d1_entry_frequency_analysis.json")
    parser.add_argument("--calibration", default="var/reporting/z_gap/calibration_samples.jsonl")
    args = parser.parse_args(argv)

    runs_dir = Path(args.runs_dir)
    if args.runs:
        run_dirs = []
        for raw in args.runs:
            p = Path(raw)
            run_dirs.append(p if p.is_dir() else runs_dir / raw)
    else:
        run_dirs = _discover_observe_runs(runs_dir)

    analysis = analyze_runs(run_dirs)
    cal_path = Path(args.calibration)
    if cal_path.is_file():
        usable = 0
        would = 0
        for line in cal_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("calibration_usable"):
                usable += 1
                if row.get("would_have_entered"):
                    would += 1
        analysis["calibration_samples_usable"] = usable
        analysis["calibration_would_have_entered"] = would

    if analysis["windows_complete"] == 0:
        analysis["confidence_level"] = "low"
        analysis["limitations"].append(
            "No local observe-only run facts found; operator should run additional observe-only windows before live enforce."
        )

    out_path = Path(args.output_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(analysis, indent=2), encoding="utf-8")
    print(json.dumps(analysis, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
