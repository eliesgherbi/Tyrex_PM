#!/usr/bin/env python3
"""N7A deterministic one-shot acceptance (FakeTransport only)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))

from helpers_n7 import fill_order, make_enter, make_exit, make_n7_host, yes_book  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="N7A fake one-shot acceptance")
    ap.add_argument("--out-dir", type=Path, default=None)
    args = ap.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or (REPO / "var" / "reporting" / "n7" / f"fixture_{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)

    host = make_n7_host(persistence_path=out_dir / "state.json")
    host.inner.preflight_reconcile()
    book = yes_book(host)
    entered = host.try_enter(make_enter(host), book=book)
    assert entered["status"] == "ACKNOWLEDGED", entered
    fill_order(host, entered["order_id"], qty="10", price="0.51", side="BUY")
    exited = host.try_exit(
        make_exit(host), book=book, limit_price=Decimal("0.49")
    )
    assert exited["status"] == "ACKNOWLEDGED", exited
    fill_order(
        host,
        exited["order_id"],
        qty=exited["exit_qty"],
        price="0.49",
        side="SELL",
    )
    post = host.inner.post_trade_reconcile()
    host.terminate(reason="fixture_complete")
    # envelope consumed once
    assert host.envelope is not None and host.envelope.consumed
    summary = {
        "mode": "n7a_fixture_acceptance",
        "live_scope": "A",
        "transport": "FakeTransport",
        "config_fingerprint": host.sealed.fingerprint(),
        "entry": entered,
        "exit": exited,
        "post_trade": post,
        "real_venue_mutations": host.inner.real_venue_mutations,
        "mutations_force_off": host.mutations_force_off,
        "terminated": host.terminated,
        "envelope_consumed": host.envelope.consumed,
        "allows_real_venue_mutation": host.envelope.allows_real_venue_mutation,
        "status": host.status(),
    }
    path = out_dir / "fixture_summary.json"
    path.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in summary if k != "status"}, indent=2)[:4000])
    ok = (
        post.get("flat") is True
        and host.inner.real_venue_mutations == 0
        and host.mutations_force_off
        and host.envelope.allows_real_venue_mutation is False
    )
    raise SystemExit(0 if ok else 2)


if __name__ == "__main__":
    main()
