"""Read-only, run-scoped qualification evidence; never infer a fill from success."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from statistics import median

import yaml

NAMES = ("B_recorded_hold", "B_restart", "D_managed_exit", "A_essential_hold")


def inspect(root, name):
    out = root / name / "out"
    report_file = out / "run_summary.json"
    if not report_file.exists():
        return {"name": name, "completed": False}
    report = json.loads(report_file.read_text())
    config = yaml.safe_load((root / (name + ".yaml")).read_text())
    database = out / "execution_evidence.sqlite3"
    if not database.exists():
        database = Path(config["state_directory"]) / "execution.sqlite3"
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        records = [(kind, at, json.loads(payload)) for kind, at, payload in db.execute(
            "select event_type,observed_at,payload_json from run_events "
            "where run_instance_id=? order by sequence", (report["run_instance_id"],))]
    reasons, windows, latency, exits, loop = Counter(), {}, [], [], []
    for kind, at, data in records:
        if kind == "STRATEGY_DECISION":
            reasons[data.get("reason_code")] += 1
            window = data.get("window_id")
            windows.setdefault(window, Counter())[data.get("reason_code")] += 1
        elif kind == "EVALUATION_SKIPPED":
            reasons["HOST_SKIPPED:" + ",".join(data.get("reasons", []))] += 1
        elif kind == "ENTRY_OUTCOME":
            latency.append({"at": at, "outcome": data.get("kind"),
                            "intent_id": data.get("intent_id"), "posted": data.get("posted"),
                            "reason": data.get("reason"), **data.get("latency", {})})
        elif kind == "EXIT_OUTCOME":
            exits.append({"at": at, **data})
        elif kind == "EVENT_LOOP_LAG_SUMMARY":
            loop.append({"at": at, **data})
    capture = report.get("recording") or {}
    seals = [json.loads(p.read_text()) for p in out.glob("seal_*.json")]
    reconstruction_file = next((root / name / folder / "reconstruction.json"
                                for folder in ("reconstruction_ordered_v1",
                                               "reconstruction_clock_v1", "reconstruction")
                                if (root / name / folder / "reconstruction.json").exists()),
                               root / name / "reconstruction/reconstruction.json")
    reconstruction = (json.loads(reconstruction_file.read_text())
                      if reconstruction_file.exists() else None)
    return {"name": name, "completed": True, "outcome": report["outcome"],
            "ok": report["ok"], "run_instance_id": report["run_instance_id"],
            "counts": report.get("counts"), "runtime_errors": report.get("runtime_errors"),
            "decision_reasons": dict(reasons),
            "window_reasons": {str(k): dict(v) for k, v in windows.items()},
            "entry_outcomes": latency, "exit_outcomes": exits, "loop_minute_summaries": loop,
            "capture": {key: capture.get(key) for key in (
                "failed", "dropped", "capture_errors", "capture_cost_us", "writer_cpu_s")},
            "seals": [{key: s.get(key) for key in (
                "window_id", "sealed_k", "availability_policy", "boundary_lag_ms",
                "source_timestamp_exact", "recovered_from_history")} for s in seals],
            "reconstruction": None if reconstruction is None else {
                "equal": reconstruction["decisions_reconstructed_equal"],
                "execution_context_verified": reconstruction.get("execution_context_verified"),
                "windows": [{"window_id": w["window_id"], "error": w.get("error"),
                             "comparison": w.get("comparison")}
                            for w in reconstruction["windows"]]}}


def main():
    root = Path(Path("var/research/trading_integrity/latest_qualification.txt").read_text())
    runs = [inspect(root, name) for name in NAMES]
    performance = {}
    for name in ("B_recorded_hold", "A_essential_hold"):
        run = next(r for r in runs if r["name"] == name)
        # Drop the first minute's startup distribution. Keep minute quantiles as
        # minute quantiles: averaging them would not produce a run-wide p99.
        minutes = run.get("loop_minute_summaries", [])[1:]
        performance[name] = {
            "steady_minutes": len(minutes),
            "minute_p50_median_ms": median([m["p50_ms"] for m in minutes]) if minutes else None,
            "minute_p99_min_ms": min([m["p99_ms"] for m in minutes]) if minutes else None,
            "minute_p99_max_ms": max([m["p99_ms"] for m in minutes]) if minutes else None,
            "entry_outcomes": len(run.get("entry_outcomes", [])),
            "posted_entries": len({o["intent_id"] for o in run.get("entry_outcomes", [])
                                   if o.get("posted")})}
    comparison = {"runs": performance,
                  "order_path_sample_sufficient": all(
                      r["posted_entries"] >= 30 for r in performance.values()),
                  "limits": ["Sequential public-market periods, not matched randomized trials.",
                             "B preceded reconstruction fixes and context timestamp recording; "
                             "A/B are not a frozen same-build overhead benchmark.",
                             "Paper submissions include simulated latency, not real HTTP.",
                             "Minute p99 range is not the run-wide p99.",
                             "No-fill runs do not qualify fill, hold or sell reconstruction."]}
    captured = [r for r in runs if r["name"] != "A_essential_hold"]
    filled = any(o["outcome"] in ("FILLED", "PARTIAL")
                 for r in runs for o in r.get("entry_outcomes", []))
    sold = any(float(o.get("sold_shares") or 0) > 0
               for r in runs for o in r.get("exit_outcomes", []))
    coverage = {
        "all_bounded_paper_runs_completed_cleanly": all(
            r.get("completed") and r.get("ok") for r in runs),
        "all_captured_runs_reconstructed_equal": all(
            (r.get("reconstruction") or {}).get("equal") is True for r in captured),
        "natural_fill_observed": filled,
        "natural_early_sale_observed": sold,
        "order_path_sample_sufficient": comparison["order_path_sample_sufficient"],
        "live_enabled": False,
        "note": "A clean no-entry run does not qualify a fill, hold across restart, or sale."}
    output = {"root": str(root), "runs": runs, "performance_screen": comparison,
              "coverage": coverage}
    (root / "review.json").write_text(json.dumps(output, indent=2), "utf-8")
    print(json.dumps({"report": str(root / "review.json"),
                      "completed": [r["name"] for r in runs if r["completed"]],
                      "performance_screen": comparison, "coverage": coverage}, indent=2))


if __name__ == "__main__":
    main()
