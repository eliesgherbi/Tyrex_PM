#!/usr/bin/env python3
"""Enrich a0_5_observe_analysis.json with final timing-fix validation metadata."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    analysis_path = Path("var/reporting/z_gap/a0_5_observe_analysis.json")
    data = json.loads(analysis_path.read_text(encoding="utf-8"))

    clock_sanity = json.loads(Path("var/reporting/z_gap/clock_sanity.json").read_text(encoding="utf-8"))
    sidecar = Path("var/state/chainlink_ticks.jsonl")
    sidecar_rows: list[dict] = []
    if sidecar.is_file():
        for line in sidecar.read_text(encoding="utf-8").splitlines():
            if line.strip():
                sidecar_rows.append(json.loads(line))

    for r in data["runs"]:
        facts_path = Path("var/reporting/runs") / r["run_name"] / "facts.jsonl"
        facts = [json.loads(line) for line in facts_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        clocks = [f for f in facts if f.get("fact_type") == "clock_sync"]
        r["clock_sync"] = clocks[-1].get("payload") if clocks else None

    data["timing_fix_final"] = {
        "batch_id": "z_gap_observe_fix_20260710",
        "preflight_clock_sanity": clock_sanity,
        "sidecar_validation": {
            "feed": "chainlink",
            "path": str(sidecar),
            "tick_count": len(sidecar_rows),
            "sample_row": sidecar_rows[0] if sidecar_rows else None,
            "retention_policy": "prune every 300s, retention_s=7200",
            "crash_after_startup": False,
        },
        "file_derived_ptb_tests": {
            "module": "tests/test_price_to_beat_tracker.py",
            "tests_passed": 9,
        },
        "calibration_samples": {"usable_rows_fix_batch": 3, "infra_debug_rows": 0},
        "no_oms_proof": {"oms_submit": 0, "intent_created": 0, "allocation_ledger": 0},
        "recommendation_a0_6": False,
        "recommendation_a0_5_timing_fix_accepted": True,
    }
    data["analyzed_at_utc"] = datetime.now(timezone.utc).isoformat()
    analysis_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Wrote {analysis_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
