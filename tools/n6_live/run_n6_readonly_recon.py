#!/usr/bin/env python3
"""N6 authenticated read-only reconciliation (mutations impossible).

Authenticates and reads account state twice (including after restart).
Never submits, cancels, redeems, or changes allowances.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from tyrex_pm.execution.polymarket.auth import assert_no_secrets, redact_text
from tyrex_pm.runtime.live_preflight import run_live_preflight
from tyrex_pm.runtime.n6_account_classify import (
    AcknowledgedExternalPosition,
    classify_account,
)

REPO = Path(__file__).resolve().parents[2]

# Operator-declared historical positions — visible, untouched, not strategy inventory.
DEFAULT_ACKNOWLEDGED = (
    AcknowledgedExternalPosition(
        label="historical_lol",
        status="RESOLVED_REDEEMABLE",
        notes="N6/N7 must not redeem or alter",
    ),
    AcknowledgedExternalPosition(
        label="historical_btc_5m_1",
        status="RESOLVED_REDEEMABLE",
        notes="N6/N7 must not redeem or alter",
    ),
    AcknowledgedExternalPosition(
        label="historical_btc_5m_2",
        status="RESOLVED_REDEEMABLE",
        notes="N6/N7 must not redeem or alter",
    ),
    AcknowledgedExternalPosition(
        label="historical_btc_5m_3",
        status="RESOLVED_REDEEMABLE",
        notes="N6/N7 must not redeem or alter",
    ),
)


def _run_once(*, out: Path, dotenv: Path | None, label: str) -> dict:
    result = run_live_preflight(
        output_path=out,
        dotenv_path=dotenv,
        user_stream_observe_s=2.0,
        skip_auth=False,
    )
    payload = dict(result.payload)
    # Strip any accidental secrets from nested dumps
    text = redact_text(json.dumps(payload))
    payload = json.loads(text)
    assert_no_secrets(text)

    positions = list(payload.get("positions") or payload.get("venue_positions") or [])
    open_orders = list(payload.get("open_orders") or [])
    selected = set(payload.get("selected_market_token_ids") or [])
    recon = dict(payload.get("reconciliation") or {})
    balance_evidence = dict(payload.get("balance_evidence") or {})
    classified = classify_account(
        open_orders=open_orders,
        positions=positions,
        selected_market_token_ids=selected,
        acknowledged=DEFAULT_ACKNOWLEDGED,
        unknown=not result.ok,
    )
    open_order_count = int(recon.get("open_order_count", len(open_orders)))
    position_count = int(recon.get("position_row_count", len(positions)))
    report = {
        "label": label,
        "ok": result.ok,
        "artifact_path": str(result.artifact_path),
        "mutations_enabled": False,
        "mutations_dispatched": 0,
        "real_venue_mutations": 0,
        "submit_called": False,
        "cancel_called": False,
        "redeem_called": False,
        "allowance_changed": False,
        "live_scope": "A",
        "classification": classified.to_dict(),
        "signer_funder": (
            payload.get("identity_mapping")
            or payload.get("identity")
            or payload.get("signer_funder")
        ),
        "user_stream": payload.get("user_stream"),
        "balances": balance_evidence or payload.get("balances") or payload.get("balance"),
        "allowances": {
            "has_allowance_field": balance_evidence.get("has_allowance_field"),
            "retrieved": balance_evidence.get("retrieved"),
        },
        "open_order_count": open_order_count,
        "position_count": position_count,
        "authenticated_ops": payload.get("authenticated_ops"),
        "reconciliation": {
            "counts": recon.get("counts"),
            "observation_only_local_empty": recon.get("observation_only_local_empty"),
            "blocks_entry_if_trading": recon.get("blocks_entry_if_trading"),
            "trade_count": recon.get("trade_count"),
        },
        "preflight_ok": result.ok,
    }
    assert_no_secrets(json.dumps(report))
    return report


def main() -> None:
    ap = argparse.ArgumentParser(
        description="N6 authenticated read-only recon (no venue mutations)"
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Fresh timestamped evidence directory",
    )
    ap.add_argument("--dotenv", type=Path, default=REPO / ".env")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or (REPO / "var" / "reporting" / "n6" / f"readonly_{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)

    dotenv = args.dotenv if args.dotenv.exists() else None
    first = _run_once(
        out=out_dir / "preflight_1.json",
        dotenv=dotenv,
        label="first_reconciliation",
    )
    second = _run_once(
        out=out_dir / "preflight_2_restart.json",
        dotenv=dotenv,
        label="restart_reconciliation",
    )

    summary = {
        "mode": "n6_authenticated_readonly",
        "not_live_trading": True,
        "mutations_enabled": False,
        "real_venue_mutations": 0,
        "orders_live": 0,
        "venue_mutation": False,
        "scope_b": False,
        "first": first,
        "restart": second,
        "classification_stable": first.get("classification") == second.get("classification"),
        "acknowledged_positions_untouched": True,
        "secrets_redacted": True,
        "out_dir": str(out_dir),
        "gate3_status": (
            "LIVE_READONLY_OK"
            if first.get("ok") and second.get("ok")
            else "BLOCKED_OR_PARTIAL"
        ),
    }
    summary_path = out_dir / "readonly_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2)[:5000])
    if summary["gate3_status"] != "LIVE_READONLY_OK":
        raise SystemExit(2)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
