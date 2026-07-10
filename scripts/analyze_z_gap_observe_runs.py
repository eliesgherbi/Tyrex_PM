#!/usr/bin/env python3
"""Analyze Z-Gap observe-only live run facts for A0.5 validation report."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


def _facts_by_type(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        ft = str(r.get("fact_type") or "")
        out.setdefault(ft, []).append(r)
    return out


def _num_range(vals: list[float]) -> dict[str, Any]:
    if not vals:
        return {"min": None, "max": None, "count": 0}
    return {"min": min(vals), "max": max(vals), "count": len(vals)}


def analyze_run(run_dir: Path) -> dict[str, Any]:
    facts_path = run_dir / "facts.jsonl"
    summary_path = run_dir / "run_summary.json"
    rows = _load_facts(facts_path)
    by_type = _facts_by_type(rows)

    terminal = by_type.get("z_gap_terminal_summary", [])
    terminal_payload = terminal[-1].get("payload", {}) if terminal else {}

    skip_hist: Counter[str] = Counter()
    for r in by_type.get("z_gap_entry_skip", []):
        code = (r.get("payload") or {}).get("reason_code")
        if code:
            skip_hist[str(code)] += 1

    basis_codes = {
        "z_gap_basis_exceeded_fresh_chainlink",
        "z_gap_chainlink_stale_basis_untrusted",
    }
    basis_seen = {c: skip_hist.get(c, 0) for c in basis_codes}

    model_facts = by_type.get("model_state_snapshot", [])
    z_vals: list[float] = []
    p_up_vals: list[float] = []
    sigma_ready = False
    jump_trips = 0
    for r in model_facts:
        p = r.get("payload") or {}
        if p.get("z") is not None:
            try:
                z_vals.append(float(p["z"]))
            except (TypeError, ValueError):
                pass
        if p.get("p_up") is not None:
            try:
                p_up_vals.append(float(p["p_up"]))
            except (TypeError, ValueError):
                pass
        if p.get("sigma_ready"):
            sigma_ready = True
        if p.get("jump_guard_tripped"):
            jump_trips += 1

    edge_facts = by_type.get("edge_evaluated", [])
    edge_up_vals: list[float] = []
    edge_down_vals: list[float] = []
    would_enter = 0
    for r in by_type.get("z_gap_entry_eval", []):
        if (r.get("payload") or {}).get("decision_status") == "would_enter":
            would_enter += 1
    for r in edge_facts:
        p = r.get("payload") or {}
        for key, bucket in (("edge_up", edge_up_vals), ("edge_down", edge_down_vals)):
            raw = p.get(key)
            if raw is not None:
                try:
                    bucket.append(float(raw))
                except (TypeError, ValueError):
                    pass

    fee_facts = by_type.get("fee_model_resolved", [])
    fee_payload = fee_facts[-1].get("payload", {}) if fee_facts else {}

    feed_health = by_type.get("signal_feed_health", [])
    binance_connected = any(
        (r.get("payload") or {}).get("feed") == "binance" and (r.get("payload") or {}).get("connected")
        for r in feed_health
    )
    chainlink_connected = any(
        (r.get("payload") or {}).get("feed") == "chainlink" and (r.get("payload") or {}).get("connected")
        for r in feed_health
    )

    ptb_facts = by_type.get("price_to_beat_observed", [])
    ptb_payload = ptb_facts[-1].get("payload", {}) if ptb_facts else {}

    forbidden = {
        "oms_submit": len(by_type.get("oms_submit", [])),
        "intent_created": len(by_type.get("intent_created", [])),
        "allocation_ledger": len(by_type.get("allocation_ledger", [])),
    }

    run_summary: dict[str, Any] = {}
    if summary_path.is_file():
        try:
            run_summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    first_ts = rows[0].get("ts") if rows else None
    last_ts = rows[-1].get("ts") if rows else None

    return {
        "run_dir": str(run_dir),
        "run_name": run_dir.name,
        "facts_path": str(facts_path),
        "fact_count": len(rows),
        "fact_types": sorted(by_type.keys()),
        "run_start_ts": first_ts,
        "run_end_ts": last_ts,
        "run_summary": run_summary,
        "terminal_summary": terminal_payload,
        "operational_pass": terminal_payload.get("operational_pass"),
        "market_id": terminal_payload.get("market_id"),
        "condition_id": terminal_payload.get("condition_id"),
        "ptb_observed": terminal_payload.get("ptb_observed"),
        "ptb_status": terminal_payload.get("ptb_status") or ptb_payload.get("ptb_status"),
        "ptb_lag_ms": terminal_payload.get("ptb_lag_ms") or ptb_payload.get("ptb_lag_ms"),
        "feed_uptime_pct": terminal_payload.get("feed_uptime_pct"),
        "sigma_ready_s": terminal_payload.get("sigma_ready_s"),
        "fee_model_id": terminal_payload.get("fee_model_id") or fee_payload.get("fee_model_id"),
        "fee_model_status": terminal_payload.get("fee_model_status") or fee_payload.get("fee_model_status"),
        "evaluation_count": terminal_payload.get("evaluation_count"),
        "would_enter_count": terminal_payload.get("would_enter_count") or would_enter,
        "skip_reason_histogram": dict(skip_hist) or terminal_payload.get("skip_reason_histogram", {}),
        "basis_code_counts": basis_seen,
        "model_snapshot_count": len(model_facts),
        "z_range": _num_range(z_vals),
        "p_up_range": _num_range(p_up_vals),
        "sigma_ready_observed": sigma_ready,
        "jump_guard_trips": jump_trips,
        "edge_up_range": _num_range(edge_up_vals),
        "edge_down_range": _num_range(edge_down_vals),
        "binance_connected": binance_connected,
        "chainlink_connected": chainlink_connected,
        "forbidden_fact_counts": forbidden,
        "calibration_sample_written": terminal_payload.get("calibration_sample_written"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze Z-Gap observe-only runs")
    parser.add_argument(
        "runs",
        nargs="+",
        help="Run directory paths or run names under var/reporting/runs/",
    )
    parser.add_argument(
        "--runs-dir",
        default="var/reporting/runs",
        help="Base runs directory when passing run names",
    )
    parser.add_argument("--output", default=None, help="Write JSON summary to path")
    args = parser.parse_args(argv)

    base = Path(args.runs_dir)
    analyses: list[dict[str, Any]] = []
    for raw in args.runs:
        p = Path(raw)
        if not p.is_dir():
            p = base / raw
        analyses.append(analyze_run(p))

    out = {"analyzed_at_utc": datetime.now(timezone.utc).isoformat(), "runs": analyses}
    text = json.dumps(out, indent=2)
    print(text)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
