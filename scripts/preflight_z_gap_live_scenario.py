#!/usr/bin/env python3
"""Preflight validator for Z-Gap tiny-live enforce scenarios (A0.8)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tyrex_pm.runtime.config import load_app_config  # noqa: E402
from tyrex_pm.runtime.z_gap_live_preflight import validate_z_gap_live_scenario  # noqa: E402
from tyrex_pm.runtime.z_gap_preflight import load_z_gap_preflight_gates  # noqa: E402


def _gate_report(artifacts: Path) -> dict[str, dict[str, object]]:
    gates = load_z_gap_preflight_gates(artifacts)
    return {
        "fee_curve": {
            "passed": gates.fee_curve_spike_passed,
            "detail": gates.details.get("fee_curve_spike"),
        },
        "binance_connectivity": {
            "passed": gates.binance_connectivity_passed,
            "detail": gates.details.get("binance_connectivity"),
        },
        "time_authority": {
            "passed": gates.clock_sanity_passed,
            "detail": gates.details.get("clock_sanity"),
        },
        "fresh_ptb_attestation": {
            "passed": gates.ptb_attestation_passed,
            "boundary_status": gates.ptb_boundary_status,
            "detail": gates.details.get("ptb_attestation"),
        },
        "calibration_review": {
            "passed": gates.calibration_lite_reviewed,
            "detail": gates.details.get("calibration_lite_review"),
        },
        "operator_approval": {
            "passed": gates.operator_approved_enforce,
            "detail": gates.details.get("operator_enforce_approval"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Z-Gap live enforce scenario preflight")
    parser.add_argument("--scenario", required=True, help="Path to scenario YAML")
    parser.add_argument("--artifacts-dir", default=None, help="Preflight artifacts directory")
    parser.add_argument("--json-out", default=None, help="Optional JSON report path")
    args = parser.parse_args()

    scenario_path = Path(args.scenario)
    if not scenario_path.is_file():
        print(f"ERROR: scenario not found: {scenario_path}")
        return 2

    try:
        scenario_file = str(scenario_path.relative_to(REPO)).replace("\\", "/")
    except ValueError:
        scenario_file = str(scenario_path).replace("\\", "/")

    from unittest.mock import patch

    with patch("tyrex_pm.runtime.z_gap_live.validate_z_gap_live_config"):
        app = load_app_config(
            repo_root=REPO,
            strategy_file="config/strategies/z_gap.yaml",
            scenario_file=scenario_file,
        )
    raw = {}
    try:
        import yaml

        raw_doc = yaml.safe_load(scenario_path.read_text(encoding="utf-8")) or {}
        raw = raw_doc.get("strategy", {}).get("z_gap", {}) if isinstance(raw_doc, dict) else {}
    except Exception:
        raw = {}

    artifacts = Path(args.artifacts_dir) if args.artifacts_dir else Path("var/reporting/z_gap")
    lifecycle_path = artifacts / "lifecycle_state.json"
    gate_report = _gate_report(artifacts)
    result = validate_z_gap_live_scenario(
        app,
        artifacts_dir=artifacts,
        raw_strategy_z_gap=raw,
        lifecycle_state_path=lifecycle_path,
    )

    report = {
        "ok": result.ok,
        "blocked": not result.ok,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
        "gates": gate_report,
        "scenario": str(scenario_path),
        "artifacts_dir": str(artifacts),
    }
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("Z-Gap live preflight gate report:")
    for name, row in gate_report.items():
        status = "PASS" if row["passed"] else "BLOCK"
        print(f"  {name}: {status}")
    if result.warnings:
        for w in result.warnings:
            print(f"WARN: {w}")
    if result.errors:
        for e in result.errors:
            print(f"ERROR: {e}")
        print(f"PREFLIGHT BLOCKED ({len(result.errors)} errors)")
        return 1
    print("PREFLIGHT OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
