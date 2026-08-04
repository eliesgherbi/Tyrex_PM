#!/usr/bin/env python3
"""N7 deterministic FakeTransport one-shot acceptance."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def main() -> None:
    ap = argparse.ArgumentParser(description="N7 fake one-shot acceptance")
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument(
        "--config",
        type=Path,
        default=REPO / "config" / "n7_tiny_live.json",
    )
    args = ap.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or (REPO / "var" / "runs" / "z_gap" / f"n7_fixture_{stamp}")
    from tyrex_pm.runtime.n7_operator_run import run_fake_oneshot_rehearsal

    result = run_fake_oneshot_rehearsal(out_dir=out_dir, config_path=args.config)
    print(result.payload.get("outcome"), result.report_path)
    raise SystemExit(0 if result.ok else 2)


if __name__ == "__main__":
    main()
