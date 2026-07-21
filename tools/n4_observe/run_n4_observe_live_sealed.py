#!/usr/bin/env python3
"""N4B live OBSERVE with sealed Chainlink K (public/read-only).

Reuses the shared live composition (discover → EXACT seal → evaluate).
No OMS. No orders. TLS always verified.
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
from tyrex_pm.runtime.n4_observe_runtime import N4ObserveRuntime

REPO = Path(__file__).resolve().parents[2]


def main() -> None:
    ap = argparse.ArgumentParser(description="N4B sealed live OBSERVE (public data)")
    ap.add_argument("--min-seals", type=int, default=3)
    ap.add_argument("--max-duration-s", type=float, default=1200.0)
    ap.add_argument("--prep-lead-s", type=float, default=45.0)
    ap.add_argument("--wait-for-boundary", action="store_true")
    ap.add_argument("--out-dir", type=Path, default=None)
    args = ap.parse_args()

    run_id = datetime.now(timezone.utc).strftime("n4b_%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or (REPO / "var" / "reporting" / "n4b" / run_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.wait_for_boundary:
        wait = seconds_until_next_boundary() - float(args.prep_lead_s)
        if wait > 1.0:
            print(json.dumps({"waiting_s": wait}, indent=2))
            import time

            time.sleep(wait)

    def on_eval(runtime: N4ObserveRuntime) -> list[dict]:
        rec = runtime.evaluate_active(trigger="feed")
        return [rec.to_dict()]

    summary = asyncio.run(
        run_live_zgap_compose(
            mode="n4_observe",
            out_dir=out_dir,
            run_id=run_id,
            min_seals=args.min_seals,
            max_duration_s=args.max_duration_s,
            prep_lead_s=args.prep_lead_s,
            on_after_seal_eval=on_eval,
            stop_when_seals_met=True,
        )
    )
    d = summary.to_dict()
    # Also write the conventional N4 summary path shape
    legacy = {
        "mode": "live",
        "n4b_status": "LIVE_OK" if d["observation_count"] > 0 else "LIVE_PARTIAL_OR_OK",
        "not_live_evidence": False,
        "tls_verify": True,
        "auth_touched": False,
        "orders_touched": False,
        "oms_touched": d["oms_touched"],
        "orders_planned": 0,
        "orders_shadow": 0,
        "orders_live": 0,
        "venue_mutation": False,
        "seals": d["seals"],
        "feeds": d["feeds"],
        "discovery": d["discovery"],
        "observation_count": d["observation_count"],
        "observations": d["observations"],
        "out_dir": str(out_dir),
        "ended_at": d["ended_at"],
    }
    (out_dir / "observe_live_summary.json").write_text(
        json.dumps(legacy, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {k: legacy[k] for k in legacy if k != "observations"},
            indent=2,
        )[:6000]
    )
    ok = (
        len(d["seals"]) >= 1
        and d["observation_count"] > 0
        and not d["oms_touched"]
        and d["orders_live"] == 0
    )
    raise SystemExit(0 if ok else 2)


if __name__ == "__main__":
    main()
