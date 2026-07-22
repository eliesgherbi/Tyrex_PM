#!/usr/bin/env python3
"""N7 operator one-shot (Scope A).

Authorization = invoking this command with ``--live``.
No envelopes, phrases, or nonces.

Without ``--live``: authenticated read-only preflight only (no mutations).
With ``--live``: preflight → one BTC 5m window → at most one entry → exit → report.

The agent must never run this with ``--live``. Operators run it locally.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "N7 tiny Scope A one-shot. "
            "Pass --live to authorize real venue mutations for one window. "
            "No approval phrase or envelope is required."
        )
    )
    ap.add_argument(
        "--live",
        action="store_true",
        help=(
            "Operator authorization: enable bounded real mutations for one "
            "BTC 5m window after preflight. Omit for read-only preflight only."
        ),
    )
    ap.add_argument(
        "--config",
        type=Path,
        default=REPO / "config" / "n7_tiny_live.json",
    )
    ap.add_argument("--dotenv", type=Path, default=REPO / ".env")
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--max-duration-s", type=float, default=300.0)
    ap.add_argument(
        "--fake-rehearsal",
        action="store_true",
        help="Run FakeTransport entry→exit→FLAT (no venue). Ignores --live.",
    )
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or (REPO / "var" / "reporting" / "n7" / f"oneshot_{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)

    from tyrex_pm.runtime.n7_operator_run import (
        run_fake_oneshot_rehearsal,
        run_operator_oneshot,
    )

    if args.fake_rehearsal:
        result = run_fake_oneshot_rehearsal(out_dir=out_dir, config_path=args.config)
    else:
        result = asyncio.run(
            run_operator_oneshot(
                repo=REPO,
                out_dir=out_dir,
                config_path=args.config,
                dotenv=args.dotenv if args.dotenv.exists() else None,
                live=bool(args.live),
                max_duration_s=float(args.max_duration_s),
            )
        )

    summary = {
        "outcome": result.outcome,
        "ok": result.ok,
        "report": str(result.report_path),
        "live": bool(args.live) and not args.fake_rehearsal,
        "real_venue_mutations": result.payload.get("real_venue_mutations", 0),
        "authorization_ceremony": "removed",
    }
    print(json.dumps(summary, indent=2))
    print(f"report={result.report_path}", file=sys.stderr)
    raise SystemExit(0 if result.ok else 2)


if __name__ == "__main__":
    main()
