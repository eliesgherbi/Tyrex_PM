#!/usr/bin/env python3
"""N6 deterministic Scope A fixture acceptance (FakeTransport only)."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

# Import helpers from tests for the acceptance driver
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))

from helpers_n6 import fill_order, make_enter_intent, make_exit_intent, make_host, yes_book  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="N6 fake-transport Scope A acceptance")
    ap.add_argument("--out-dir", type=Path, default=None)
    args = ap.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or (REPO / "var" / "reporting" / "n6" / f"fixture_{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)

    host = make_host(persistence_path=out_dir / "state.json")
    host.preflight_reconcile()
    book = yes_book(host)
    entered = host.try_enter(make_enter_intent(host), book=book)
    assert entered["status"] == "ACKNOWLEDGED", entered
    oid = entered["order_id"]
    from tyrex_pm.core.ids import OrderId

    fill_order(host, OrderId(oid), qty="5", price="0.51", side="BUY")
    assert host.lifecycle.state.value == "ACTIVE"
    exited = host.try_exit(
        make_exit_intent(host), book=book, limit_price=__import__("decimal").Decimal("0.49")
    )
    assert exited["status"] == "ACKNOWLEDGED", exited
    fill_order(
        host,
        OrderId(exited["order_id"]),
        qty=exited["exit_qty"],
        price="0.49",
        side="SELL",
    )
    post = host.post_trade_reconcile()
    summary = {
        "mode": "n6_fixture_acceptance",
        "live_scope": "A",
        "transport": "FakeTransport",
        "entry": entered,
        "exit": exited,
        "post_trade": post,
        "real_venue_mutations": host.real_venue_mutations,
        "facts": [{"type": t, "payload": p} for t, p in host.facts],
        "status": host.status(),
    }
    path = out_dir / "fixture_summary.json"
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in summary if k != "facts"}, indent=2)[:4000])
    ok = (
        post.get("flat") is True
        and host.real_venue_mutations == 0
        and entered["status"] == "ACKNOWLEDGED"
    )
    raise SystemExit(0 if ok else 2)


if __name__ == "__main__":
    main()
