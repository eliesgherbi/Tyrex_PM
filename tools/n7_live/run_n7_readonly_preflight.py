#!/usr/bin/env python3
"""N7 authenticated read-only preflight (mutations impossible)."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from tyrex_pm.runtime.n7_preflight import run_n7_preflight

REPO = Path(__file__).resolve().parents[2]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="N7 read-only preflight (no mutations; no authorization ceremony)"
    )
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument(
        "--config",
        type=Path,
        default=REPO / "config" / "n7_tiny_live.json",
    )
    ap.add_argument("--dotenv", type=Path, default=REPO / ".env")
    ap.add_argument("--user-stream-s", type=float, default=2.0)
    args = ap.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or (REPO / "var" / "runs" / "_ops" / "n7_preflight" / f"readonly_{stamp}")
    result = run_n7_preflight(
        out_dir=out_dir,
        config_path=args.config,
        repo=REPO,
        dotenv=args.dotenv if args.dotenv.exists() else None,
        user_stream_observe_s=args.user_stream_s,
        require_clean_worktree=False,
    )
    print(json.dumps(result.payload, indent=2)[:6000])
    raise SystemExit(0 if result.ok else 2)


if __name__ == "__main__":
    main()
