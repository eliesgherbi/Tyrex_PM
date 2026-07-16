#!/usr/bin/env python3
"""A0.8 shadow enforce E2E dry-run — produces facts + terminal summary (no live orders)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tyrex_pm.strategies.z_gap.scenario_oms import OmsFillSpec, ScenarioOMS  # noqa: E402
from tyrex_pm.strategies.z_gap.shadow_harness import ShadowHarness, TickSpec  # noqa: E402


async def _run(out_dir: Path) -> int:
    event_start = time.time() + 120
    event_end = event_start + 300
    oms = ScenarioOMS(
        buy_fills=[OmsFillSpec(status="matched", taking_amount="8")],
        sell_fills=[OmsFillSpec(status="matched", making_amount="8")],
    )
    harness = ShadowHarness.create(
        tmp_path=out_dir,
        event_start_ts=event_start,
        event_end_ts=event_end,
        oms=oms,
        exit_cfg={"stop_confirm_s": 0, "retry_interval_ms": 0, "z_stop": "0.05"},
    )
    result = await harness.run_ticks(
        [
            TickSpec(now_ts=event_start + 90, binance_price=Decimal("100150")),
            TickSpec(now_ts=event_start + 91, binance_price=Decimal("100150")),
            TickSpec(now_ts=event_start + 92, binance_price=Decimal("99800")),
            TickSpec(now_ts=event_start + 93, binance_price=Decimal("99800")),
        ]
    )
    facts_path = out_dir / "facts.jsonl"
    with facts_path.open("w", encoding="utf-8") as fh:
        for fact in result.facts:
            fh.write(json.dumps(fact) + "\n")
    summary_path = out_dir / "terminal_summary.json"
    summary = harness.terminal_summary() or {}
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"exit_code": result.exit_code, "terminal": summary, "facts": str(facts_path)}, indent=2))
    return result.exit_code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="var/reporting/runs/z_gap_a0_8_shadow_e2e")
    args = parser.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    return asyncio.run(_run(out))


if __name__ == "__main__":
    raise SystemExit(main())
