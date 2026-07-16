"""R6B authenticated read-only probe — never submits or cancels.

Usage:
  python scripts/r6b_readonly_probe.py

Reads L2 credentials from environment / .env (via os.environ).
Writes a sanitized JSON report under var/reporting/r6/ (gitignored).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Load .env into os.environ without printing values
env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from tyrex_pm.execution.polymarket.auth import (  # noqa: E402
    CredentialError,
    assert_no_secrets,
    load_l2_credentials,
)
from tyrex_pm.execution.polymarket.readonly_client import (  # noqa: E402
    MutationAttemptError,
    ReadOnlyClobClient,
)
from tyrex_pm.execution.polymarket.reconciliation import ReconciliationService  # noqa: E402
from tyrex_pm.execution.order_store import OrderStore  # noqa: E402
from tyrex_pm.execution.fill_ledger import FillLedger  # noqa: E402
from tyrex_pm.portfolio.portfolio import Portfolio  # noqa: E402


def main() -> int:
    report: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mutations_attempted": False,
        "operations": [],
    }
    try:
        creds = load_l2_credentials()
        report["credentials"] = "present"
        report["address_prefix"] = creds.address[:6] + "…"
    except CredentialError as exc:
        report["credentials"] = "missing"
        report["error"] = str(exc)
        _write(report)
        print("R6B: credentials missing — fail closed (no mutation).")
        return 2

    client = ReadOnlyClobClient(creds=creds)
    # Prove mutation surfaces are blocked
    try:
        from tyrex_pm.execution.polymarket.transport import SubmitOrderRequest

        client.submit_order(
            SubmitOrderRequest(token_id="x", side="BUY", price="0.5", size="1")
        )
        report["mutations_attempted"] = True
        report["error"] = "submit_order unexpectedly succeeded"
        _write(report)
        return 3
    except MutationAttemptError:
        report["operations"].append({"op": "submit_order", "result": "blocked"})

    try:
        client.cancel_order("0xdead")
        report["mutations_attempted"] = True
        report["error"] = "cancel_order unexpectedly succeeded"
        _write(report)
        return 3
    except MutationAttemptError:
        report["operations"].append({"op": "cancel_order", "result": "blocked"})

    opens = []
    trades = []
    positions = []
    clob_blocked = False

    # Data API positions (address-scoped, non-mutating)
    try:
        positions = client.get_positions()
        report["operations"].append(
            {
                "op": "get_positions",
                "count": len(positions),
                "nonzero": sum(1 for p in positions if p.size != 0),
                "result": "ok",
            }
        )
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        assert_no_secrets(msg, creds)
        report["operations"].append(
            {"op": "get_positions", "result": "error", "error": type(exc).__name__}
        )

    # CLOB L2 reads — may be Cloudflare-blocked from some networks
    for op_name, fn in (
        ("get_open_orders", client.get_open_orders),
        ("get_trades", client.get_trades),
        ("get_balance", client.get_balance),
    ):
        try:
            val = fn()
            if op_name == "get_open_orders":
                opens = val
                report["operations"].append(
                    {"op": op_name, "count": len(val), "result": "ok"}
                )
            elif op_name == "get_trades":
                trades = val
                report["operations"].append(
                    {"op": op_name, "count": len(val), "result": "ok"}
                )
            else:
                report["operations"].append(
                    {
                        "op": op_name,
                        "has_balance_field": True,
                        "result": "ok",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            assert_no_secrets(msg, creds)
            clob_blocked = True
            report["operations"].append(
                {
                    "op": op_name,
                    "result": "blocked_or_error",
                    "error": type(exc).__name__ + ": " + msg[:120],
                }
            )

    orders = OrderStore()
    ledger = FillLedger()
    portfolio = Portfolio(fill_ledger=ledger)
    recon = ReconciliationService(order_store=orders, portfolio=portfolio)
    result = recon.reconcile(
        venue_orders=opens,
        venue_trades=trades[:20],
        venue_positions=positions,
        missing_evidence=clob_blocked,
    )
    report["reconciliation"] = {
        "counts": result.counts(),
        "blocks_entry": result.blocks_entry,
        "requires_manual": result.requires_manual,
        "clob_l2_blocked": clob_blocked,
    }
    if clob_blocked:
        report["readiness"] = "blocked_clob_l2_unreachable"
        report["blocker"] = (
            "Authenticated CLOB L2 HTTP reads returned Cloudflare/network denial "
            "(error 1010 class). Mutations remain blocked. Data-API positions may "
            "still succeed. User-stream not opened in R6B automated probe."
        )
    elif result.blocks_entry:
        report["readiness"] = "blocked_external_or_mismatch"
    else:
        report["readiness"] = "clean_relative_to_empty_local"

    raw = json.dumps(report, indent=2)
    assert_no_secrets(raw, creds)
    path = _write(report)
    print(f"R6B: read-only probe complete — report {path}")
    print(f"  readiness={report['readiness']} mutations_attempted=False")
    return 0


def _write(report: dict) -> Path:
    out = ROOT / "var" / "reporting" / "r6" / "readonly_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return out


if __name__ == "__main__":
    raise SystemExit(main())
