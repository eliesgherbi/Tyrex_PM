"""Build compact no-entry shutdown summary from run facts."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any

POST_ENTRY_PHASES = frozenset(
    {
        "BOTH_ENTRY_PENDING",
        "YES_ENTRY_PENDING",
        "NO_ENTRY_PENDING",
        "BOTH_LEGS_FILLED",
        "BOTH_LEGS_ACTIVE",
        "ACTIVATION_PENDING_RECHECK",
        "ONLY_YES_ACTIVE",
        "ONLY_NO_ACTIVE",
        "STOP_PENDING_YES",
        "STOP_PENDING_NO",
        "TP_PENDING_YES",
        "TP_PENDING_NO",
        "TIMEOUT_PENDING",
        "EXITING_YES",
        "EXITING_NO",
        "EXITING_BOTH",
        "DONE",
    }
)


def _dec(raw: object) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except Exception:
        return None


def had_pair_entry(rows: list[dict[str, Any]], final_state: str) -> bool:
    types = {str(r.get("fact_type")) for r in rows}
    if "paired_binary_pair_entry_committed" in types:
        return True
    if final_state in POST_ENTRY_PHASES and final_state not in {"DONE"}:
        return True
    for row in rows:
        if row.get("fact_type") != "paired_binary_state_change":
            continue
        payload = row.get("payload") or {}
        to_state = payload.get("state") or payload.get("to_state")
        if to_state in {"BOTH_ENTRY_PENDING", "BOTH_LEGS_FILLED", "BOTH_LEGS_ACTIVE"}:
            return True
    return False


def build_no_entry_summary(
    rows: list[dict[str, Any]],
    *,
    run_id: str,
    market_id: str,
    yes_token_id: str,
    no_token_id: str,
    ticks: int,
    duration_s: float | None,
    final_state: str,
    last_market_timing_phase: str | None,
) -> dict[str, Any] | None:
    if had_pair_entry(rows, final_state):
        return None

    skip_reasons: Counter[str] = Counter()
    risk_denials: Counter[str] = Counter()
    quality_rejects: Counter[str] = Counter()
    readiness_blocks: Counter[str] = Counter()
    pair_costs: list[Decimal] = []
    entry_eval_count = 0
    entry_planned_count = 0
    pair_entry_submit_count = 0
    oms_submit_count = 0
    last_yes_bid = last_yes_ask = last_no_bid = last_no_ask = None
    pair_cost_last = None
    final_readiness_state = None
    final_quality_verdict = None

    for row in rows:
        ft = str(row.get("fact_type"))
        payload = row.get("payload") or {}

        if ft == "paired_binary_entry_eval":
            entry_eval_count += 1
            pc = _dec(payload.get("pair_cost"))
            if pc is not None:
                pair_costs.append(pc)
                pair_cost_last = pc
        elif ft == "paired_binary_entry_skip":
            reason = str(payload.get("reason") or "unknown")
            skip_reasons[reason] += 1
            pc = _dec(payload.get("pair_cost"))
            if pc is not None:
                pair_costs.append(pc)
                pair_cost_last = pc
        elif ft == "paired_binary_state_change":
            if payload.get("state") == "ENTRY_PLANNED":
                entry_planned_count += 1
        elif ft == "paired_binary_pair_entry_committed":
            pair_entry_submit_count += 1
        elif ft == "paired_binary_entry_submitted":
            pair_entry_submit_count += 1
        elif ft == "oms_submit":
            oms_submit_count += 1
        elif ft == "paired_binary_entry_leg_blocked":
            codes = payload.get("reason_codes") or []
            if isinstance(codes, list):
                for code in codes:
                    risk_denials[str(code)] += 1
            else:
                risk_denials[str(codes)] += 1
        elif ft == "risk_decision":
            codes = payload.get("reason_codes") or []
            if isinstance(codes, list):
                for code in codes:
                    if str(code) != "approved":
                        risk_denials[str(code)] += 1
        elif ft == "data_quality_verdict":
            verdict = str(payload.get("verdict") or "")
            if verdict and verdict.lower() != "pass":
                final_quality_verdict = verdict
            for reason in payload.get("reasons") or []:
                quality_rejects[str(reason)] += 1
        elif ft == "market_data_health_block":
            reason = str(payload.get("block_reason") or payload.get("reason") or "unknown")
            readiness_blocks[reason] += 1
            rs = payload.get("readiness_state")
            if rs:
                final_readiness_state = str(rs)
        elif ft == "market_readiness_transition":
            final_readiness_state = str(payload.get("to") or payload.get("to_state") or final_readiness_state)

        if payload.get("yes_bid") is not None:
            last_yes_bid = payload.get("yes_bid")
        if payload.get("yes_ask") is not None:
            last_yes_ask = payload.get("yes_ask")
        if payload.get("no_bid") is not None:
            last_no_bid = payload.get("no_bid")
        if payload.get("no_ask") is not None:
            last_no_ask = payload.get("no_ask")

    pair_cost_min = str(min(pair_costs)) if pair_costs else None
    pair_cost_max = str(max(pair_costs)) if pair_costs else None

    return {
        "run_id": run_id,
        "market_id": market_id,
        "yes_token_id": yes_token_id,
        "no_token_id": no_token_id,
        "ticks": ticks,
        "duration_s": duration_s,
        "entry_eval_count": entry_eval_count,
        "entry_planned_count": entry_planned_count,
        "pair_entry_submit_count": pair_entry_submit_count,
        "oms_submit_count": oms_submit_count,
        "top_skip_reasons": dict(skip_reasons.most_common(10)),
        "risk_denial_reasons": dict(risk_denials.most_common(10)),
        "quality_reject_reasons": dict(quality_rejects.most_common(10)),
        "readiness_block_reasons": dict(readiness_blocks.most_common(10)),
        "pair_cost_min": pair_cost_min,
        "pair_cost_max": pair_cost_max,
        "pair_cost_last": str(pair_cost_last) if pair_cost_last is not None else None,
        "last_yes_bid": last_yes_bid,
        "last_yes_ask": last_yes_ask,
        "last_no_bid": last_no_bid,
        "last_no_ask": last_no_ask,
        "last_market_timing_phase": last_market_timing_phase,
        "final_readiness_state": final_readiness_state,
        "final_quality_verdict": final_quality_verdict,
        "final_state": final_state,
    }
