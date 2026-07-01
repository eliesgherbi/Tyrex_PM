#!/usr/bin/env python3
"""Validate a Phase 2 WS-primary paired-binary live sanity run.

Thin wrapper around validate_m8_ws_primary_run.py — same acceptance criteria
apply (WS-primary backbone, freshness, planner evidence, latency chain, etc.).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
M8_VALIDATOR = REPO / "scripts" / "validate_m8_ws_primary_run.py"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate Phase 2 WS-primary paired-binary live run facts"
    )
    parser.add_argument("run_dir", type=Path, help="Path to run directory with facts.jsonl")
    parser.add_argument("--json-out", type=Path, help="Write JSON summary")
    parser.add_argument("--md-out", type=Path, help="Write markdown summary")
    args = parser.parse_args()

    run_dir = args.run_dir if args.run_dir.is_absolute() else REPO / args.run_dir
    cmd = [sys.executable, str(M8_VALIDATOR), str(run_dir)]
    if args.json_out:
        cmd.extend(["--json-out", str(args.json_out)])
    if args.md_out:
        cmd.extend(["--md-out", str(args.md_out)])

    print(f"Phase 2 live validation (delegating to {M8_VALIDATOR.name})")
    print(f"  run_dir: {run_dir}")
    proc = subprocess.run(cmd, cwd=str(REPO))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
