"""Manual reconciliation script tests."""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_reconcile_pm_ui_example(tmp_path: Path) -> None:
    facts = tmp_path / "facts.jsonl"
    payload = {
        "yes_entry_cash": "2.40",
        "no_entry_cash": "2.65",
        "yes_exit_cash": "3.95",
        "no_exit_cash": "1.20",
        "yes_entry_qty": "5",
        "no_entry_qty": "5",
        "yes_exit_qty": "5",
        "no_exit_qty": "5",
        "yes_entry_cash_source": "oms_match_evidence",
        "no_entry_cash_source": "oms_match_evidence",
        "yes_exit_cash_source": "oms_match_evidence",
        "no_exit_cash_source": "oms_match_evidence",
        "pnl_total": "0.10",
        "pnl_status": "tentative",
    }
    facts.write_text(
        json.dumps({"fact_type": "paired_binary_realized_pnl_tentative", "payload": payload}) + "\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "reconcile_run_cashflows.py"),
            "--facts",
            str(facts),
            "--manual-fill",
            "yes_buy=0.497,5",
            "--manual-fill",
            "no_buy=0.547,5",
            "--manual-fill",
            "no_sell=0.227,5",
            "--manual-fill",
            "yes_sell=0.778,5",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        check=True,
    )
    report = json.loads(proc.stdout)
    assert report["local_bot_pnl"] == "0.10"
    manual_pnl = Decimal(report["manual_reconciled_pnl"])
    assert abs(manual_pnl - Decimal("-0.195")) < Decimal("0.01")
    assert len(report["price_deltas"]) >= 1
