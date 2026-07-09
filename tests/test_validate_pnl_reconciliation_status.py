"""Validator PnL reconciliation status classification."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.validate_paired_binary_phase2_live_run import (  # noqa: E402
    _pnl_reconciliation_status,
    _pnl_reported,
)


def _row(fact_type: str, payload: dict) -> dict:
    return {"fact_type": fact_type, "payload": payload}


def test_legacy_oms_pnl_classified_tentative() -> None:
    rows = [
        _row(
            "paired_binary_realized_pnl",
            {
                "pnl_total": "0.10",
                "yes_entry_cash_source": "oms_match_evidence",
                "no_entry_cash_source": "oms_match_evidence",
                "yes_exit_cash_source": "oms_match_evidence",
                "no_exit_cash_source": "oms_match_evidence",
            },
        )
    ]
    info = _pnl_reconciliation_status(rows)
    assert info["pnl_status"] == "tentative"
    assert info["pnl_blocker_for_enforcement"] is True
    ok, detail = _pnl_reported(rows)
    assert ok is True
    assert detail == "0.10"


def test_final_pnl_not_enforcement_blocker() -> None:
    rows = [
        _row(
            "paired_binary_realized_pnl",
            {
                "pnl_total": "-0.195",
                "pnl_status": "final",
                "yes_entry_cash_source": "venue_reconciled",
            },
        )
    ]
    info = _pnl_reconciliation_status(rows)
    assert info["pnl_status"] == "final"
    assert info["pnl_blocker_for_enforcement"] is False


def test_tentative_fact_reported() -> None:
    rows = [_row("paired_binary_realized_pnl_tentative", {"pnl_total": "0.10", "pnl_status": "tentative"})]
    ok, detail = _pnl_reported(rows)
    assert ok is True
    assert detail == "0.10"
    info = _pnl_reconciliation_status(rows)
    assert info["pnl_status"] == "tentative"


def test_discrepancy_blocks_enforcement() -> None:
    rows = [
        _row("paired_binary_realized_pnl", {"pnl_total": "-0.195", "pnl_status": "final"}),
        _row("oms_fill_discrepancy_detected", {"leg": "yes", "cash_delta": "0.09"}),
    ]
    info = _pnl_reconciliation_status(rows)
    assert info["pnl_blocker_for_enforcement"] is True
    assert info["has_discrepancy"] is True
