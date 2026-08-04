#!/usr/bin/env python3
"""N3B live PTB attestation + immutable seal (public/read-only).

Discovers BTC 5m markets, waits for Chainlink EXACT_AT_START candidates,
attests against SSR displayed openPrice (comparison only), and seals K.

Evidence is written under a fresh run directory — never appends into prior
N1 capture JSONL files.

Never submits orders. Never disables TLS. Never fabricates K.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from tyrex_pm.runtime.live_zgap_compose import (
    run_live_zgap_compose,
    seconds_until_next_boundary,
)

REPO = Path(__file__).resolve().parents[2]


def main() -> None:
    ap = argparse.ArgumentParser(description="N3B live PTB seal (public data only)")
    ap.add_argument("--min-seals", type=int, default=3, help="Stop after N sealed windows")
    ap.add_argument(
        "--max-duration-s",
        type=float,
        default=1200.0,
        help="Hard cap (default 20m for ≥3 boundaries with lead)",
    )
    ap.add_argument(
        "--prep-lead-s",
        type=float,
        default=45.0,
        help="Informational lead before boundary (ops hint)",
    )
    ap.add_argument(
        "--wait-for-boundary",
        action="store_true",
        help="Sleep until ~prep-lead before the next 5m boundary before starting feeds",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Fresh evidence directory (default var/runs/_ops/n3_validation/<run_id>)",
    )
    args = ap.parse_args()

    run_id = datetime.now(timezone.utc).strftime("n3_%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or (REPO / "var" / "runs" / "_ops" / "n3_validation" / run_id)
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.wait_for_boundary:
        lead = max(0.0, float(args.prep_lead_s))
        wait = seconds_until_next_boundary() - lead
        if wait > 1.0:
            print(json.dumps({"waiting_s": wait, "prep_lead_s": lead}, indent=2))
            import time

            time.sleep(wait)

    summary = asyncio.run(
        run_live_zgap_compose(
            mode="n3_seal",
            out_dir=out_dir,
            run_id=run_id,
            min_seals=args.min_seals,
            max_duration_s=args.max_duration_s,
            prep_lead_s=args.prep_lead_s,
            stop_when_seals_met=True,
        )
    )
    compact = {
        k: summary.to_dict()[k]
        for k in (
            "mode",
            "run_id",
            "out_dir",
            "started_at",
            "ended_at",
            "feeds",
            "discovery",
            "seals",
            "missed_windows",
            "errors",
            "gate_notes",
            "auth_touched",
            "orders_live",
            "venue_mutation",
        )
    }
    print(json.dumps(compact, indent=2)[:8000])
    seals = summary.seals
    ok = (
        len(seals) >= args.min_seals
        and all(s.get("immutable_check_ok") for s in seals)
        and all(s.get("boundary_rule") == "EXACT_AT_START" for s in seals)
        and summary.orders_live == 0
        and not summary.venue_mutation
    )
    raise SystemExit(0 if ok else 2)


if __name__ == "__main__":
    main()
