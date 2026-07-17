#!/usr/bin/env python3
"""Read-only R7C incident reconciliation. Never submits or cancels."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure src on path when run as script
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tyrex_pm.runtime.r7c_incident_recon import run_incident_recon  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="R7C read-only incident / FLAT recon")
    p.add_argument(
        "--output",
        type=Path,
        default=Path("var/reporting/r7c/incident_recon.json"),
    )
    p.add_argument(
        "--buy-order-id",
        default="0x68efa63a23abb0ab55042204683f48f4303ed2db3e9d955317bc41add43e71db",
    )
    p.add_argument(
        "--condition-id",
        default="0x32204a5cffff255df6155b69105aded512770c4597ccb4bef721dfa0ab526401",
    )
    p.add_argument(
        "--token-id",
        default=(
            "1038082852808592687103030316298741805143710778541582307413776216436336466979"
        ),
    )
    p.add_argument(
        "--market-slug",
        default="btc-updown-5m-1784303100",
    )
    p.add_argument(
        "--ack-path",
        type=Path,
        default=Path("var/reporting/r7/r7a2_position_acknowledgment.json"),
    )
    args = p.parse_args()
    report = run_incident_recon(
        buy_order_id=args.buy_order_id,
        condition_id=args.condition_id,
        token_id=args.token_id,
        market_slug=args.market_slug,
        acknowledgment_path=args.ack_path if args.ack_path.exists() else None,
        output_path=args.output,
    )
    print(json.dumps({k: report[k] for k in report if k != "raw_trades_sample"}, indent=2))
    print(f"wrote={args.output}")
    return 0 if report.get("selected_market_flat") else 3


if __name__ == "__main__":
    raise SystemExit(main())
