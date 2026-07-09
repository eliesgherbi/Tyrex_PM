#!/usr/bin/env python3
"""Comprehensive review of paired-binary WS-primary live runs."""

from __future__ import annotations

import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.validate_paired_binary_phase2_live_run import analyze_phase2  # noqa: E402


def _d(x: object) -> Decimal | None:
    if x is None or x == "":
        return None
    try:
        return Decimal(str(x))
    except Exception:
        return None


def load_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def had_pair_entry(rows: list[dict]) -> bool:
    types = {r.get("fact_type") for r in rows}
    if "paired_binary_pair_entry_committed" in types:
        return True
    for row in rows:
        if row.get("fact_type") != "paired_binary_state_change":
            continue
        payload = row.get("payload") or {}
        state = payload.get("state") or payload.get("to_state")
        if state in {"BOTH_ENTRY_PENDING", "BOTH_LEGS_FILLED", "BOTH_LEGS_ACTIVE"}:
            return True
    return False


def last_health(rows: list[dict]) -> dict | None:
    for row in reversed(rows):
        if row.get("fact_type") != "health":
            continue
        return row.get("payload") or {}
    return None


def terminal_exit_summary(rows: list[dict]) -> dict:
    resolution = next(
        (r for r in rows if r.get("fact_type") == "paired_binary_resolution_exit_accounting"),
        None,
    )
    pnl_unavail = next(
        (r for r in rows if r.get("fact_type") == "paired_binary_realized_pnl_unavailable"),
        None,
    )
    res_payload = (resolution or {}).get("payload") or {}
    pnl_payload = (pnl_unavail or {}).get("payload") or {}
    terminal_reason = res_payload.get("terminal_reason")
    pnl_status = res_payload.get("pnl_status") or pnl_payload.get("pnl_status")
    if terminal_reason is None and pnl_payload.get("reason") == "resolution_cashflow_missing":
        terminal_reason = "market_resolution_without_oms_exit"
        pnl_status = pnl_status or "resolution_cashflow_missing"
    elif terminal_reason is None and pnl_payload.get("reason") == "missing_exit_cashflow":
        terminal_reason = "missing_exit_cashflow"
        pnl_status = pnl_status or "unavailable"
    return {
        "terminal_reason": terminal_reason,
        "pnl_status": pnl_status,
        "manual_reconciliation_required": bool(
            res_payload.get("manual_reconciliation_required")
            or pnl_payload.get("manual_reconciliation_required")
        ),
    }


def analyze_no_entry(rows: list[dict]) -> dict:
    nes = next((r for r in reversed(rows) if r.get("fact_type") == "paired_binary_no_entry_summary"), None)
    summary = (nes or {}).get("payload") or {}
    mt_rows = [r for r in rows if r.get("fact_type") == "paired_binary_market_timing"]
    mt_first = (mt_rows[0].get("payload") or {}) if mt_rows else {}
    mt_last = (mt_rows[-1].get("payload") or {}) if mt_rows else {}
    skips = Counter(
        (r.get("payload") or {}).get("reason")
        for r in rows
        if r.get("fact_type") == "paired_binary_entry_skip"
    )
    preflight = Counter()
    for row in rows:
        if row.get("fact_type") != "paired_binary_pair_preflight_rejected":
            continue
        for code in (row.get("payload") or {}).get("reason_codes") or []:
            preflight[str(code)] += 1
    readiness = [
        {
            "from": (r.get("payload") or {}).get("from"),
            "to": (r.get("payload") or {}).get("to"),
            "reason": (r.get("payload") or {}).get("reason"),
        }
        for r in rows
        if r.get("fact_type") == "market_readiness_transition"
    ]
    health_blocks = Counter(
        (r.get("payload") or {}).get("block_reason") or (r.get("payload") or {}).get("reason")
        for r in rows
        if r.get("fact_type") == "market_data_health_block"
    )
    quality = Counter()
    for row in rows:
        if row.get("fact_type") != "data_quality_verdict":
            continue
        payload = row.get("payload") or {}
        if str(payload.get("verdict", "")).lower() != "pass":
            for reason in payload.get("reasons") or []:
                quality[str(reason)] += 1
    ws_cutover = any(r.get("fact_type") == "ws_primary_cutover" for r in rows)
    rest_disabled = any(r.get("fact_type") == "rest_poll_disabled" for r in rows)
    return {
        "summary": summary,
        "market_timing_first": mt_first,
        "market_timing_last": mt_last,
        "market_timing_phases": dict(Counter((r.get("payload") or {}).get("phase") for r in mt_rows)),
        "entry_skip_reasons": dict(skips.most_common(15)),
        "preflight_rejects": dict(preflight.most_common(15)),
        "readiness_transitions": readiness,
        "health_blocks": dict(health_blocks.most_common(10)),
        "quality_rejects": dict(quality.most_common(10)),
        "ws_primary_cutover": ws_cutover,
        "rest_poll_disabled": rest_disabled,
        "entry_eval_count": sum(1 for r in rows if r.get("fact_type") == "paired_binary_entry_eval"),
        "pair_entry_committed": sum(1 for r in rows if r.get("fact_type") == "paired_binary_pair_entry_committed"),
    }


def _survival_advisory_section(rows: list[dict]) -> dict:
    try:
        from tyrex_pm.strategies.paired_binary.terminal_reporting import (
            activation_unwind_failure_pattern,
            survivor_phase_reached,
        )
    except ImportError:
        return {}
    reached = survivor_phase_reached(rows)
    if activation_unwind_failure_pattern(rows) or not reached:
        return {
            "survivor_phase_reached": reached,
            "survival_advisory_not_exercised": True,
            "survival_advisory_not_exercised_reason": (
                "activation_failed_before_survivor"
                if activation_unwind_failure_pattern(rows)
                else "survivor_phase_not_reached"
            ),
            "survival_advisory_applicable": False,
        }
    return {
        "survivor_phase_reached": True,
        "survival_advisory_not_exercised": False,
        "survival_advisory_applicable": True,
    }


def _hardening_section(rows: list[dict]) -> dict:
    terminal = next(
        (r.get("payload") or {} for r in reversed(rows) if r.get("fact_type") == "paired_binary_terminal_summary"),
        None,
    )
    mt = next(
        (r.get("payload") or {} for r in rows if r.get("fact_type") == "paired_binary_market_timing"),
        {},
    )
    loop = next(
        (
            (r.get("payload") or {})
            for r in reversed(rows)
            if r.get("fact_type") == "health" and (r.get("payload") or {}).get("event") == "paired_binary_loop_stopped"
        ),
        {},
    )
    return {
        "hardening_metadata_ok": bool(mt.get("event_end_ts") and mt.get("market_id")),
        "lifecycle_clock_known": mt.get("event_end_ts") not in (None, ""),
        "one_shot_shutdown_observed": any(
            r.get("fact_type") == "strategy_terminal_safe_to_stop" for r in rows
        ),
        "terminal_summary_present": terminal is not None,
        "terminal_reason": (terminal or {}).get("terminal_reason"),
        "final_state": loop.get("final_state") or (terminal or {}).get("final_state"),
    }


def _phase1_fact_highlights(rows: list[dict]) -> dict:
    reachability = [
        (r.get("payload") or {}).get("reachability_verdict")
        for r in rows
        if r.get("fact_type") == "survivor_reachability_scored"
    ]
    return {
        "stall_detected": any(r.get("fact_type") == "survivor_stall_detected" for r in rows),
        "reachability_verdicts": sorted({v for v in reachability if v}),
        "trailing_armed": any(r.get("fact_type") == "survivor_trailing_stop_armed" for r in rows),
        "trailing_triggered": any(r.get("fact_type") == "survivor_trailing_stop_triggered" for r in rows),
        "economics_negative": any(
            (r.get("payload") or {}).get("economics_verdict") in {"exit_survivor_early", "negative", "reject"}
            for r in rows
            if r.get("fact_type") == "survivor_economics_evaluated"
        ),
        "kill_switch_triggered": any(r.get("fact_type") == "kill_switch_triggered" for r in rows),
    }


def analyze_lifecycle(rows: list[dict]) -> dict:
    pnl_plan = next((r for r in rows if r.get("fact_type") == "paired_binary_pnl_plan"), None)
    stop_plan = next((r for r in rows if r.get("fact_type") == "paired_binary_stop_plan"), None)
    tp_plan = next((r for r in rows if r.get("fact_type") == "paired_binary_winner_target_plan"), None)
    activation = next((r for r in rows if r.get("fact_type") == "paired_binary_monitor_started"), None)
    realized = next(
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
    done = next((r for r in rows if r.get("fact_type") == "paired_binary_done"), None)

    entries = {}
    for row in rows:
        if row.get("fact_type") != "paired_binary_state_change":
            continue
        payload = row.get("payload") or {}
        if payload.get("state") == "BOTH_LEGS_ACTIVE":
            entries = {
                "yes_entry": payload.get("yes_entry"),
                "no_entry": payload.get("no_entry"),
                "pair_cost": payload.get("pair_cost"),
                "yes_bid": payload.get("yes_bid"),
                "yes_ask": payload.get("yes_ask"),
                "no_bid": payload.get("no_bid"),
                "no_ask": payload.get("no_ask"),
            }
            break

    stops = []
    for row in rows:
        if row.get("fact_type") != "paired_binary_leg_stop":
            continue
        p = row.get("payload") or {}
        stops.append(
            {
                "leg": p.get("leg") or p.get("active_leg"),
                "trigger_bid": p.get("trigger_bid") or p.get("bid"),
                "planned_stop": p.get("planned_stop") or p.get("yes_planned_stop") or p.get("no_planned_stop"),
                "entry": p.get("yes_entry") if (p.get("leg") or p.get("active_leg")) == "yes" else p.get("no_entry"),
                "reason": p.get("reason"),
            }
        )

    tps = []
    for row in rows:
        if row.get("fact_type") != "paired_binary_winner_target":
            continue
        p = row.get("payload") or {}
        tps.append(
            {
                "leg": p.get("leg") or p.get("survivor_leg") or p.get("active_leg"),
                "trigger_bid": p.get("trigger_bid") or p.get("bid"),
                "target_price": p.get("target_price") or p.get("winner_target"),
                "reason": p.get("reason"),
            }
        )

    repriced = [
        (r.get("payload") or {})
        for r in rows
        if r.get("fact_type") == "paired_binary_winner_target_repriced"
    ]

    oms = []
    for row in rows:
        if row.get("fact_type") != "oms_submit":
            continue
        p = row.get("payload") or {}
        oms.append(
            {
                "side": p.get("side"),
                "token_id": str(p.get("token_id", ""))[-8:],
                "price": p.get("price"),
                "size": p.get("size"),
                "source": p.get("source") or p.get("book_source"),
            }
        )

    pp = (pnl_plan or {}).get("payload") or {}
    pair_cost = _d(pp.get("pair_cost") or entries.get("pair_cost"))
    loss_budget = _d(pp.get("loss_budget"))
    profit_budget = _d(pp.get("profit_budget"))
    yes_entry = _d(entries.get("yes_entry") or pp.get("yes_entry"))
    no_entry = _d(entries.get("no_entry") or pp.get("no_entry"))
    yes_stop = _d(pp.get("yes_planned_stop"))
    no_stop = _d(pp.get("no_planned_stop"))
    yes_target = _d(pp.get("yes_planned_target"))
    no_target = _d(pp.get("no_planned_target"))

    math_check = {}
    if pair_cost and yes_entry and no_entry:
        sl_pct = Decimal("0.09")
        tp_pct = Decimal("0.3")
        buf = Decimal("0.005")
        expected_loss = pair_cost * sl_pct
        expected_profit = pair_cost * tp_pct
        math_check = {
            "pair_cost": str(pair_cost),
            "expected_loss_budget_9pct": str(expected_loss.quantize(Decimal("0.0001"))),
            "emitted_loss_budget": str(loss_budget),
            "expected_profit_budget_30pct": str(expected_profit.quantize(Decimal("0.0001"))),
            "emitted_profit_budget": str(profit_budget),
            "expected_yes_stop": str((yes_entry - expected_loss - buf).quantize(Decimal("0.0001"))) if yes_stop else None,
            "emitted_yes_stop": str(yes_stop),
            "expected_no_stop": str((no_entry - expected_loss - buf).quantize(Decimal("0.0001"))) if no_stop else None,
            "emitted_no_stop": str(no_stop),
            "expected_yes_target": str((yes_entry + expected_profit + buf).quantize(Decimal("0.0001"))) if yes_target else None,
            "emitted_yes_target": str(yes_target),
            "expected_no_target": str((no_entry + expected_profit + buf).quantize(Decimal("0.0001"))) if no_target else None,
            "emitted_no_target": str(no_target),
        }

    rp = (realized or {}).get("payload") or {}
    return {
        "entries": entries,
        "pnl_plan": pp,
        "stop_plan": (stop_plan or {}).get("payload") or {},
        "tp_plan": (tp_plan or {}).get("payload") or {},
        "activation": (activation or {}).get("payload") or {},
        "stops": stops,
        "tps": tps,
        "repriced_targets": repriced,
        "realized_pnl": rp,
        "done": (done or {}).get("payload") or {},
        "oms_submits": oms,
        "math_check": math_check,
    }


def main() -> None:
    runs_dir = REPO / "var" / "reporting" / "runs"
    runs = sorted(d for d in runs_dir.iterdir() if d.is_dir() and "paired_binary_ws_primary_live" in d.name)
    report: dict = {"runs": []}
    for run_dir in runs:
        facts_path = run_dir / "facts.jsonl"
        if not facts_path.is_file():
            continue
        rows = load_rows(facts_path)
        health = last_health(rows) or {}
        entered = had_pair_entry(rows)
        phase2 = analyze_phase2(run_dir)
        item = {
            "run_name": run_dir.name,
            "fact_count": len(rows),
            "loop_event": health.get("event"),
            "final_state": health.get("final_state"),
            "ticks": health.get("ticks"),
            "had_pair_entry": entered,
            "phase2_classification": phase2.get("classification"),
            "phase2_missing": phase2.get("missing_requirements"),
            "phase1_survival_enabled": phase2.get("phase1_survival_enabled", False),
            "survival_facts_present": phase2.get("phase1_survival_facts_present") or [],
            "phase1_classification": phase2.get("phase1_classification"),
            "runtime_premature_exit_detected": phase2.get("runtime_premature_exit_detected", False),
        }
        item.update(_phase1_fact_highlights(rows))
        item.update(_survival_advisory_section(rows))
        item.update(_hardening_section(rows))
        item.update(terminal_exit_summary(rows))
        if entered:
            item["lifecycle"] = analyze_lifecycle(rows)
        else:
            item["no_entry"] = analyze_no_entry(rows)
        report["runs"].append(item)

    out = REPO / "var" / "reporting" / "live_runs_review.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
