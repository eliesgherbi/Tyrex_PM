"""Check the R1–R5 release candidate before an operator runs --live.

Does not submit orders and does not read .env values.

Usage (Git Bash, repo root):

  python scripts/check_release_candidate.py --config config/runs/ask70_protection_tiny_live.yaml
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SHA = "599ea944179de8dbfaf1b9a062fe79f9ac54b95c"
sys.path.insert(0, str(ROOT / "src"))


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr.strip() or "git failed", file=sys.stderr)
        raise SystemExit(2)
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check the R1-R5 release candidate before --live"
    )
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = args.config if args.config.is_absolute() else ROOT / args.config

    head = _git("rev-parse", "HEAD")
    if head != EXPECTED_SHA:
        print(f"refusing: HEAD {head} != {EXPECTED_SHA}", file=sys.stderr)
        return 2
    allowed = "scripts/check_release_candidate.py"
    unexpected = [
        line
        for line in _git("status", "--porcelain").splitlines()
        if line[3:].replace("\\", "/") != allowed
    ]
    if unexpected:
        print("refusing: working tree is dirty", file=sys.stderr)
        for line in unexpected:
            print(line, file=sys.stderr)
        return 2
    if not (ROOT / ".env").is_file():
        print("refusing: .env is missing", file=sys.stderr)
        return 2
    if not config.is_file():
        print(f"refusing: missing config {config}", file=sys.stderr)
        return 2

    from tyrex_pm.runtime.run_config import load_trading_run_config

    loaded = load_trading_run_config(config)
    if loaded.risk.maximum_total_debit != Decimal("5"):
        print("refusing: maximum_total_debit is not 5", file=sys.stderr)
        return 2

    check = subprocess.run(
        [
            sys.executable,
            "-m",
            "tyrex_pm.application.cli",
            "run",
            "--config",
            str(config),
            "--validate-config",
        ],
        cwd=ROOT,
        check=False,
    )
    if check.returncode != 0:
        return check.returncode
    print(f"release candidate ok {head}")
    print("this script does not trade; run the explicit --live command next")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
