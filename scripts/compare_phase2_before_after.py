#!/usr/bin/env python3
"""Compare Phase 2 control (REST-era) vs treatment (WS-primary) paired-binary runs."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
RECONSTRUCTED_PATH = (
    REPO / "Docs" / "Implementation" / "WebSocket_event_driven_backbone" / "baseline_reconstructed_controls.json"
)


def _load_metrics(run_dir: Path, *, provenance: str = "VERIFIED") -> dict[str, Any]:
    script = REPO / "scripts" / "extract_paired_binary_metrics.py"
    proc = subprocess.run(
        [sys.executable, str(script), str(run_dir), "--provenance", provenance],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"extract failed for {run_dir}: {proc.stderr}")
    return json.loads(proc.stdout)


def _load_reconstructed(run_id: str) -> dict[str, Any]:
    data = json.loads(RECONSTRUCTED_PATH.read_text(encoding="utf-8"))
    if run_id not in data:
        raise KeyError(run_id)
    m = dict(data[run_id])
    m["run_id"] = run_id
    m["run_name"] = run_id
    return m


def _median(values: list[float]) -> float | None:
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    if not nums:
        return None
    nums.sort()
    mid = len(nums) // 2
    if len(nums) % 2:
        return nums[mid]
    return (nums[mid - 1] + nums[mid]) / 2


def _collect_num(metrics_list: list[dict], *path: str) -> list[Any]:
    out: list[Any] = []
    for m in metrics_list:
        cur: Any = m
        for p in path:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(p)
        out.append(cur)
    return out


def compare(control: list[dict[str, Any]], treatment: list[dict[str, Any]]) -> dict[str, Any]:
    def row(metric: str, ctrl_vals: list[Any], treat_vals: list[Any]) -> dict[str, Any]:
        ctrl_nums = [v for v in ctrl_vals if isinstance(v, (int, float))]
        treat_nums = [v for v in treat_vals if isinstance(v, (int, float))]
        improved = None
        if ctrl_nums and treat_nums:
            improved = _median(treat_nums) < _median(ctrl_nums)
        return {
            "metric": metric,
            "control_values": ctrl_vals,
            "treatment_values": treat_vals,
            "control_median": _median(ctrl_nums),
            "treatment_median": _median(treat_nums),
            "treatment_improved": improved,
        }

    tables = {
        "freshness": [
            row("activation_book_age_ms p95", _collect_num(control, "activation_book_age_ms", "p95"), _collect_num(treatment, "activation_book_age_ms", "p95")),
            row("entry_book_age_ms p95", _collect_num(control, "entry_book_age_ms", "p95"), _collect_num(treatment, "entry_book_age_ms", "p95")),
            row("exit_book_age_ms p95", _collect_num(control, "exit_book_age_ms", "p95"), _collect_num(treatment, "exit_book_age_ms", "p95")),
            row("all book_age_ms p95", _collect_num(control, "book_age_ms", "p95"), _collect_num(treatment, "book_age_ms", "p95")),
        ],
        "latency": [
            row(
                "submit_to_ack_ms non_null_count",
                _collect_num(control, "latency_ms", "submit_to_ack_ms", "non_null_count"),
                _collect_num(treatment, "latency_ms", "submit_to_ack_ms", "non_null_count"),
            ),
            row(
                "trigger_to_fill_ms non_null_count",
                _collect_num(control, "latency_ms", "trigger_to_fill_ms", "non_null_count"),
                _collect_num(treatment, "latency_ms", "trigger_to_fill_ms", "non_null_count"),
            ),
        ],
        "execution": [
            row("fak_reject_count", _collect_num(control, "fak_reject_count"), _collect_num(treatment, "fak_reject_count")),
            row("planner_evidence_count", _collect_num(control, "planner_evidence_count"), _collect_num(treatment, "planner_evidence_count")),
            row("planner_evidence_incomplete", _collect_num(control, "planner_evidence_incomplete"), _collect_num(treatment, "planner_evidence_incomplete")),
        ],
        "rest_safety": [
            row(
                "rest_sourced_oms_submit_count",
                _collect_num(control, "rest_sourced_oms_submit_count"),
                _collect_num(treatment, "rest_sourced_oms_submit_count"),
            ),
        ],
        "lifecycle": [
            row("survivor_timeout (bool)", _collect_num(control, "survivor_timeout"), _collect_num(treatment, "survivor_timeout")),
        ],
    }

    ctrl_prov = [m.get("provenance") for m in control]
    treat_prov = [m.get("provenance") for m in treatment]
    limitations: list[str] = []
    if any(p != "VERIFIED" for p in ctrl_prov):
        limitations.append("Control set includes RECONSTRUCTED runs — quantitative comparison is partial.")
    if len(treatment) < 3:
        limitations.append("Fewer than 3 treatment runs — widen confidence caveat.")

    return {
        "control_runs": [{"run_id": m.get("run_id"), "run_name": m.get("run_name"), "provenance": m.get("provenance"), "backbone": m.get("backbone")} for m in control],
        "treatment_runs": [{"run_id": m.get("run_id"), "run_name": m.get("run_name"), "provenance": m.get("provenance"), "backbone": m.get("backbone")} for m in treatment],
        "limitations": limitations,
        "comparison_tables": tables,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2 before/after comparison")
    parser.add_argument(
        "--control",
        action="append",
        default=[],
        help="Control run dir or run_id (reconstructed if missing dir)",
    )
    parser.add_argument("--treatment", action="append", default=[], help="Treatment run directory")
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args()

    controls: list[dict[str, Any]] = []
    for spec in args.control or [
        "paired_binary_live_1782741234",
        "paired_binary_live_1782742788",
        "paired_binary_live_1782737121",
    ]:
        p = Path(spec)
        if not p.is_absolute():
            p = REPO / "var" / "reporting" / "runs" / spec
        if p.is_dir() and (p / "facts.jsonl").is_file():
            controls.append(_load_metrics(p, provenance="VERIFIED"))
        else:
            rid = p.name if p.name else spec
            controls.append(_load_reconstructed(rid))

    treatments: list[dict[str, Any]] = []
    for spec in args.treatment or [
        "m8_validation_002",
        "m8_validation_002b",
        "m8_validation_002c",
    ]:
        p = Path(spec)
        if not p.is_absolute():
            p = REPO / "var" / "reporting" / "runs" / spec
        if not (p / "facts.jsonl").is_file():
            print(f"WARNING: skipping missing treatment {p}", file=sys.stderr)
            continue
        treatments.append(_load_metrics(p, provenance="VERIFIED"))

    summary = compare(controls, treatments)
    summary["control_metrics"] = controls
    summary["treatment_metrics"] = treatments

    out = args.json_out if args.json_out.is_absolute() else REPO / args.json_out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if treatments else 1


if __name__ == "__main__":
    raise SystemExit(main())
