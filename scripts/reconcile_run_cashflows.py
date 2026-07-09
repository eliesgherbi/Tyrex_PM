#!/usr/bin/env python3
"""Compare bot-reported cashflows vs manual Polymarket UI fills for reconciliation."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from tyrex_pm.execution.fill_reconciliation import (
    FillReconciliationConfig,
    compute_reconciled_pnl,
    manual_cashflows_from_fills,
    reconcile_paired_binary_cashflows,
)
from tyrex_pm.strategies.paired_binary.pnl import realized_pnl_from_cashflows
from tyrex_pm.strategies.paired_binary.state import LegRuntime, PairedBinaryPhase, PairedBinaryRuntimeState
from tyrex_pm.runtime.cashflows import SOURCE_OMS_MATCH_EVIDENCE


def _load_facts(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _state_from_pnl_fact(payload: dict) -> PairedBinaryRuntimeState:
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.DONE,
        effective_qty=Decimal(str(payload.get("effective_pair_qty") or payload.get("yes_qty") or "5")),
    )
    q = Decimal(str(payload.get("yes_entry_qty") or "5"))

    def _leg(prefix: str) -> LegRuntime:
        leg = LegRuntime()
        leg.entry_cash = Decimal(str(payload[f"{prefix}_entry_cash"]))
        leg.entry_qty = Decimal(str(payload.get(f"{prefix}_entry_qty") or q))
        leg.entry_cash_source = payload.get(f"{prefix}_entry_cash_source") or SOURCE_OMS_MATCH_EVIDENCE
        leg.exit_cash = Decimal(str(payload[f"{prefix}_exit_cash"]))
        leg.exit_qty = Decimal(str(payload.get(f"{prefix}_exit_qty") or q))
        leg.exit_cash_source = payload.get(f"{prefix}_exit_cash_source") or SOURCE_OMS_MATCH_EVIDENCE
        return leg

    state.yes = _leg("yes")
    state.no = _leg("no")
    return state


def _parse_manual_fill(spec: str) -> tuple[str, dict[str, str]]:
    key, rest = spec.split("=", 1)
    price_s, qty_s = rest.split(",", 1)
    return key.strip(), {"price": price_s.strip(), "qty": qty_s.strip()}


def _pnl_from_manual(manual: dict[str, dict[str, str]]) -> Decimal:
    cashflows = manual_cashflows_from_fills(manual)
    result = compute_reconciled_pnl(cashflows, effective_qty=Decimal(manual["yes_buy"]["qty"]))
    assert result.pnl_total is not None
    return result.pnl_total


def _deltas(local: dict, manual: dict[str, dict[str, str]]) -> list[dict]:
    cfg = FillReconciliationConfig()
    out = []
    mapping = {
        "yes_buy": ("yes", "entry"),
        "no_buy": ("no", "entry"),
        "yes_sell": ("yes", "exit"),
        "no_sell": ("no", "exit"),
    }
    for key, (leg, side) in mapping.items():
        m = manual[key]
        mp = Decimal(m["price"])
        mq = Decimal(m["qty"])
        mcash = mp * mq
        if side == "entry":
            lp = Decimal(str(local[f"{leg}_entry_cash"])) / mq
            lcash = Decimal(str(local[f"{leg}_entry_cash"]))
        else:
            lp = Decimal(str(local[f"{leg}_exit_cash"])) / mq
            lcash = Decimal(str(local[f"{leg}_exit_cash"]))
        price_delta = abs(lp - mp)
        cash_delta = abs(lcash - mcash)
        if price_delta > cfg.price_tolerance or cash_delta > cfg.cash_tolerance_usd:
            out.append(
                {
                    "slot": key,
                    "local_price": str(lp),
                    "manual_price": str(mp),
                    "local_cash": str(lcash),
                    "manual_cash": str(mcash),
                    "price_delta": str(price_delta),
                    "cash_delta": str(cash_delta),
                }
            )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile bot cashflows vs manual PM UI fills")
    parser.add_argument("--facts", type=Path, required=True, help="Path to facts.jsonl")
    parser.add_argument(
        "--manual-fill",
        action="append",
        default=[],
        help="Manual fill: yes_buy=0.497,5",
    )
    parser.add_argument("--manual-json", type=Path, help="JSON file with manual fills")
    args = parser.parse_args()

    manual: dict[str, dict[str, str]] = {}
    if args.manual_json:
        manual = json.loads(args.manual_json.read_text(encoding="utf-8"))
    for spec in args.manual_fill:
        k, v = _parse_manual_fill(spec)
        manual[k] = v

    rows = _load_facts(args.facts)
    pnl_row = next(
        (
            r
            for r in rows
            if r.get("fact_type")
            in (
                "paired_binary_realized_pnl",
                "paired_binary_realized_pnl_tentative",
            )
        ),
        None,
    )
    if pnl_row is None:
        print(json.dumps({"error": "no paired_binary_realized_pnl fact found"}, indent=2))
        return 1

    payload = pnl_row.get("payload") or {}
    state = _state_from_pnl_fact(payload)
    local = realized_pnl_from_cashflows(state)
    report: dict = {
        "local_bot_pnl": str(local.pnl_total) if local else None,
        "local_pnl_status": payload.get("pnl_status"),
    }

    if manual:
        if not all(k in manual for k in ("yes_buy", "no_buy", "yes_sell", "no_sell")):
            print(json.dumps({"error": "manual fills require yes_buy, no_buy, yes_sell, no_sell"}, indent=2))
            return 1
        manual_pnl = _pnl_from_manual(manual)
        report["manual_reconciled_pnl"] = str(manual_pnl)
        report["price_deltas"] = _deltas(payload, manual)
        report["discrepancy_report"] = {
            "pnl_gap": str(manual_pnl - (local.pnl_total if local else Decimal("0"))),
            "legs": report["price_deltas"],
        }

    cashflows = reconcile_paired_binary_cashflows(state)
    reconciled = compute_reconciled_pnl(cashflows, effective_qty=state.effective_qty)
    report["reconciled_status"] = reconciled.status
    report["reconciled_pnl"] = str(reconciled.pnl_total) if reconciled.pnl_total is not None else None

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
