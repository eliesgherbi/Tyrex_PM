#!/usr/bin/env python3
"""Validate a Phase 2 WS-primary paired-binary live sanity run.

Extends M8 WS-primary analysis with Phase 2 lifecycle classifications:

- PHASE2_WS_LIFECYCLE_PASS — normal entry → activation → stop/TP → paired_binary_done
- PHASE2_WS_OPEN_EXPOSURE_FORCE_FLATTEN_PASS — max_runtime open exposure → force flatten → flat DONE
- PHASE2_WS_OBSERVATION_PASS — partial / observation-only (no full lifecycle or flatten pass)
- PHASE2_WS_FAIL — failed or residual exposure
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.validate_m8_ws_primary_run import analyze as analyze_m8  # noqa: E402

try:
    from tyrex_pm.strategies.paired_binary.terminal_reporting import (  # noqa: E402
        activation_unwind_failure_pattern,
        survivor_phase_reached,
    )
except ImportError:
    def activation_unwind_failure_pattern(rows: list[dict[str, Any]]) -> bool:  # type: ignore[misc]
        return False

    def survivor_phase_reached(rows: list[dict[str, Any]]) -> bool:  # type: ignore[misc]
        return False

try:
    from tyrex_pm.reporting.schema_v2 import (  # noqa: E402
        PHASE1_SURVIVAL_EVIDENCE_KEYS,
        PHASE1_SURVIVAL_FACT_TYPES,
    )
except ImportError:
    PHASE1_SURVIVAL_FACT_TYPES = ()
    PHASE1_SURVIVAL_EVIDENCE_KEYS = ()

PREMATURE_FLATTEN_REASON_MARKERS = (
    "max_runtime",
    "fallback_max_runtime",
    "tick_budget",
    "survivor_on_max_runtime",
)

PHASE2_WS_LIFECYCLE_PASS = "PHASE2_WS_LIFECYCLE_PASS"
PHASE2_WS_OPEN_EXPOSURE_FORCE_FLATTEN_PASS = "PHASE2_WS_OPEN_EXPOSURE_FORCE_FLATTEN_PASS"
PHASE2_WS_OBSERVATION_PASS = "PHASE2_WS_OBSERVATION_PASS"
PHASE2_WS_FAIL = "PHASE2_WS_FAIL"
PHASE2_WS_ACTIVATION_UNWIND_FAIL = "PHASE2_WS_ACTIVATION_UNWIND_FAIL"

PHASE1_SURVIVAL_PASS = "PHASE1_SURVIVAL_PASS"
PHASE1_ADVISORY_PASS = "PHASE1_ADVISORY_PASS"
PHASE1_TARGET_POLICY_PASS = "PHASE1_TARGET_POLICY_PASS"
PHASE1_TRAILING_ENFORCE_PASS = "PHASE1_TRAILING_ENFORCE_PASS"
PHASE1_TRAILING_QUALITY_REJECT_PENDING_FAIL = "PHASE1_TRAILING_QUALITY_REJECT_PENDING_FAIL"
PHASE1_STALL_ENFORCE_PASS = "PHASE1_STALL_ENFORCE_PASS"
PHASE1_ENFORCE_SAFETY_FAIL = "PHASE1_ENFORCE_SAFETY_FAIL"
PHASE1_ORDER_POLICY_SAFETY_FAIL = "PHASE1_ORDER_POLICY_SAFETY_FAIL"
PHASE1_RUNTIME_PREMATURE_EXIT = "PHASE1_RUNTIME_PREMATURE_EXIT"

FORCE_FLATTEN_FACTS = (
    "paired_binary_open_exposure_at_shutdown",
    "paired_binary_shutdown_force_flatten_started",
    "paired_binary_shutdown_force_flatten_done",
)


def _load_rows(facts_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in facts_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _fact_types(rows: list[dict[str, Any]]) -> set[str]:
    return {str(r.get("fact_type")) for r in rows}


def _loop_health_event(rows: list[dict[str, Any]], event: str) -> dict[str, Any] | None:
    for row in reversed(rows):
        if row.get("fact_type") != "health":
            continue
        payload = row.get("payload") or {}
        if payload.get("event") == event:
            return payload
    return None


def _loop_stopped(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return _loop_health_event(rows, "paired_binary_loop_stopped")


def _terminal_summary(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in reversed(rows):
        if row.get("fact_type") == "paired_binary_terminal_summary":
            return row.get("payload") or {}
    return None


def _hardening_metadata_ok(rows: list[dict[str, Any]]) -> bool:
    mt = None
    for row in rows:
        if row.get("fact_type") == "paired_binary_market_timing":
            mt = row.get("payload") or {}
            break
    if mt is None:
        return False
    market_id = str(mt.get("market_id") or "")
    if not market_id or market_id in {"shadow_test_market", "test_market"}:
        return False
    if mt.get("event_start_ts") in (None, "") or mt.get("event_end_ts") in (None, ""):
        return False
    if str(mt.get("phase") or "").lower() == "unknown":
        return False
    return True


def _one_shot_shutdown_observed(rows: list[dict[str, Any]]) -> bool:
    return any(r.get("fact_type") == "strategy_terminal_safe_to_stop" for r in rows)


def _activation_unwind_fail_diagnostics(rows: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    types = _fact_types(rows)
    if "paired_binary_terminal_summary" not in types:
        missing.append("missing_fact:paired_binary_terminal_summary")
    pnl_ok, _ = _pnl_reported(rows)
    pnl_info = _pnl_reconciliation_status(rows)
    if not pnl_info["pnl_reported"]:
        missing.append("missing_pnl_fact_or_unavailable_reason")
    loop = _loop_stopped(rows)
    if loop is None:
        missing.append("missing_health:paired_binary_loop_stopped")
    elif str(loop.get("final_state", "")).upper() != "FAILED":
        missing.append(f"final_state_not_failed:{loop.get('final_state')}")
    return missing


def _no_entry_summary(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in reversed(rows):
        if row.get("fact_type") == "paired_binary_no_entry_summary":
            return row.get("payload") or {}
    return None


def _persist_error_failures(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for row in rows:
        if row.get("fact_type") != "paired_binary_state_persist_failed":
            continue
        payload = row.get("payload") or {}
        if str(payload.get("severity", "")).lower() == "error":
            failures.append(payload)
    return failures


def _no_entry_observation_pass(rows: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """Return (pass, missing_requirements) for no-buy WS-primary observation runs."""
    missing: list[str] = []
    types = _fact_types(rows)
    summary = _no_entry_summary(rows)
    if summary is None:
        missing.append("missing_fact:paired_binary_no_entry_summary")

    if "ws_primary_cutover" not in types:
        missing.append("missing_fact:ws_primary_cutover")
    if "rest_poll_disabled" not in types:
        missing.append("missing_fact:rest_poll_disabled")

    if _rest_sourced_oms_submits(rows):
        missing.append("rest_sourced_oms_submit")

    if _loop_health_event(rows, "paired_binary_loop_failed") is not None:
        missing.append("loop_failed")

    if _persist_error_failures(rows):
        missing.append("state_persist_failed_severity_error")

    if "paired_binary_pair_entry_committed" in types:
        missing.append("unexpected_pair_entry_committed")

    return len(missing) == 0, missing


def _qty_zero(raw: object) -> bool:
    if raw is None:
        return True
    try:
        return Decimal(str(raw)) <= 0
    except Exception:
        return False


def _rest_sourced_oms_submits(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for row in rows:
        if row.get("fact_type") != "oms_submit":
            continue
        payload = row.get("payload") or {}
        if str(payload.get("source") or "").lower() == "rest":
            violations.append(payload)
    return violations


def _shutdown_urgent_exit_snapshots(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snaps = []
    for row in rows:
        if row.get("fact_type") != "decision_snapshot":
            continue
        payload = row.get("payload") or {}
        if str(payload.get("decision_type")) == "urgent_exit":
            snaps.append(payload)
    return snaps


def _pnl_reconciliation_status(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify PnL reconciliation from facts (supports legacy runs)."""
    types = _fact_types(rows)
    discrepancies = [r for r in rows if r.get("fact_type") == "oms_fill_discrepancy_detected"]

    def _legacy_tentative(payload: dict[str, Any]) -> bool:
        if payload.get("pnl_status"):
            return payload.get("pnl_status") != "final"
        sources = (
            payload.get("yes_entry_cash_source"),
            payload.get("no_entry_cash_source"),
            payload.get("yes_exit_cash_source"),
            payload.get("no_exit_cash_source"),
        )
        return all(s in (None, "oms_match_evidence", "oms_ack") for s in sources)

    for row in rows:
        ft = row.get("fact_type")
        payload = row.get("payload") or {}
        if ft == "paired_binary_realized_pnl":
            status = payload.get("pnl_status") or ("tentative" if _legacy_tentative(payload) else "final")
            return {
                "pnl_status": status,
                "pnl_total": payload.get("pnl_total"),
                "pnl_reported": True,
                "pnl_final": status == "final" and not discrepancies,
                "pnl_blocker_for_enforcement": status != "final" or bool(discrepancies),
                "has_discrepancy": bool(discrepancies),
            }
        if ft == "paired_binary_realized_pnl_tentative":
            return {
                "pnl_status": "tentative",
                "pnl_total": payload.get("pnl_total"),
                "pnl_reported": True,
                "pnl_final": False,
                "pnl_blocker_for_enforcement": True,
                "has_discrepancy": bool(discrepancies),
            }
        if ft == "paired_binary_realized_pnl_unavailable":
            return {
                "pnl_status": "unavailable",
                "pnl_total": None,
                "pnl_reported": True,
                "pnl_final": False,
                "pnl_blocker_for_enforcement": True,
                "has_discrepancy": bool(discrepancies),
                "reason": payload.get("reason"),
            }
    return {
        "pnl_status": None,
        "pnl_total": None,
        "pnl_reported": False,
        "pnl_final": False,
        "pnl_blocker_for_enforcement": True,
        "has_discrepancy": bool(discrepancies),
    }


def _pnl_reported(rows: list[dict[str, Any]]) -> tuple[bool, str | None]:
    info = _pnl_reconciliation_status(rows)
    if not info["pnl_reported"]:
        return False, None
    if info["pnl_status"] == "unavailable":
        return False, str(info.get("reason") or "unavailable")
    return True, str(info["pnl_total"]) if info["pnl_total"] is not None else None


def _force_flatten_pass(rows: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """Return (pass, missing_requirements)."""
    missing: list[str] = []
    types = _fact_types(rows)

    for fact in FORCE_FLATTEN_FACTS:
        if fact not in types:
            missing.append(f"missing_fact:{fact}")

    loop = _loop_stopped(rows)
    if loop is None:
        missing.append("missing_health:paired_binary_loop_stopped")
    elif str(loop.get("final_state", "")).upper() != "DONE":
        missing.append(f"final_state_not_done:{loop.get('final_state')}")

    flatten_done = [r for r in rows if r.get("fact_type") == "paired_binary_shutdown_force_flatten_done"]
    if flatten_done:
        snap = flatten_done[-1].get("payload") or {}
        if not (_qty_zero(snap.get("yes_qty")) and _qty_zero(snap.get("no_qty"))):
            missing.append("residual_exposure_in_force_flatten_done")
    else:
        missing.append("missing_fact:paired_binary_shutdown_force_flatten_done")

    if _rest_sourced_oms_submits(rows):
        missing.append("rest_sourced_oms_submit")

    if not _shutdown_urgent_exit_snapshots(rows):
        missing.append("missing_decision_snapshot:urgent_exit")

    if "paired_binary_done" not in types:
        missing.append("missing_fact:paired_binary_done")

    pnl_ok, _ = _pnl_reported(rows)
    pnl_info = _pnl_reconciliation_status(rows)
    if not pnl_info["pnl_reported"]:
        missing.append("missing_pnl_fact_or_unavailable_reason")

    oms_submits = [r for r in rows if r.get("fact_type") == "oms_submit"]
    if oms_submits and "execution_planner_evidence" not in types:
        missing.append("missing_execution_planner_evidence")

    if oms_submits and "latency_chain" not in types:
        missing.append("missing_latency_chain")

    return len(missing) == 0, missing


def _normal_lifecycle_pass(rows: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    missing: list[str] = []
    types = _fact_types(rows)

    if "paired_binary_done" not in types:
        missing.append("missing_fact:paired_binary_done")

    has_activation = "paired_binary_monitor_started" in types or any(
        (r.get("payload") or {}).get("state") == "BOTH_LEGS_ACTIVE"
        for r in rows
        if r.get("fact_type") == "paired_binary_state_change"
    )
    if not has_activation:
        missing.append("missing_activation")

    has_exit = any(
        r.get("fact_type") in ("paired_binary_leg_stop", "paired_binary_winner_target")
        for r in rows
    )
    if not has_exit:
        missing.append("missing_stop_or_tp")

    if FORCE_FLATTEN_FACTS[0] in types:
        missing.append("shutdown_force_flatten_present")

    loop = _loop_stopped(rows)
    if loop is None or str(loop.get("final_state", "")).upper() != "DONE":
        missing.append("final_state_not_done")

    pnl_ok, _ = _pnl_reported(rows)
    pnl_info = _pnl_reconciliation_status(rows)
    if not pnl_info["pnl_reported"]:
        missing.append("missing_pnl_fact_or_unavailable_reason")

    return len(missing) == 0, missing


def _manual_intervention_shutdown(rows: list[dict[str, Any]]) -> bool:
    types = _fact_types(rows)
    if "paired_binary_shutdown_force_flatten_failed" in types:
        return True
    if any(
        r.get("fact_type") == "paired_binary_manual_intervention_required"
        and (r.get("payload") or {}).get("reason") in {
            "max_runtime_open_exposure",
            "open_exposure_timeout_elapsed",
            "shutdown_force_flatten_timeout",
        }
        for r in rows
    ):
        return True
    flatten_done = [r for r in rows if r.get("fact_type") == "paired_binary_shutdown_force_flatten_done"]
    if flatten_done:
        snap = flatten_done[-1].get("payload") or {}
        if not (_qty_zero(snap.get("yes_qty")) and _qty_zero(snap.get("no_qty"))):
            return True
    loop = _loop_stopped(rows)
    if loop and str(loop.get("final_state", "")).upper() == "FAILED":
        return True
    return False


def _load_manifest(run_dir: Path) -> dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _survival_enabled(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> bool:
    runtime = manifest.get("runtime") or {}
    survival = runtime.get("survival") or manifest.get("survival") or {}
    if bool(survival.get("enabled")):
        return True
    if PHASE1_SURVIVAL_FACT_TYPES:
        types = _fact_types(rows)
        return any(ft in types for ft in PHASE1_SURVIVAL_FACT_TYPES)
    return False


def _event_end_ts(rows: list[dict[str, Any]]) -> float | None:
    for row in reversed(rows):
        if row.get("fact_type") not in {
            "paired_binary_market_timing",
            "strategy_runtime_decision",
        }:
            continue
        payload = row.get("payload") or {}
        raw = payload.get("event_end_ts")
        if raw not in (None, ""):
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
    return None


def _flatten_before_event_end_s(rows: list[dict[str, Any]]) -> float:
    for row in reversed(rows):
        if row.get("fact_type") != "strategy_lifecycle_pre_close_flatten_required":
            continue
        payload = row.get("payload") or {}
        raw = payload.get("flatten_before_event_end_s")
        if raw not in (None, ""):
            try:
                return float(raw)
            except (TypeError, ValueError):
                break
    return 20.0


def _detect_premature_runtime_exit(rows: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    event_end = _event_end_ts(rows)
    if event_end is None:
        return False, []
    flatten_before = _flatten_before_event_end_s(rows)
    cutoff = event_end - flatten_before
    for row in rows:
        ft = row.get("fact_type")
        if ft not in {
            "paired_binary_open_exposure_at_shutdown",
            "paired_binary_open_survivor_at_max_runtime",
            "paired_binary_shutdown_force_flatten_started",
        }:
            continue
        payload = row.get("payload") or {}
        reason = str(payload.get("reason") or payload.get("shutdown_reason") or "").lower()
        if not any(marker in reason for marker in PREMATURE_FLATTEN_REASON_MARKERS):
            continue
        ts_raw = row.get("ts")
        if ts_raw in (None, ""):
            continue
        try:
            ts = float(ts_raw)
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            return True, [f"premature_runtime_flatten:{reason}@ts<{cutoff}"]
    return False, []


def _survival_fact_contract_violations(rows: list[dict[str, Any]]) -> list[str]:
    if not PHASE1_SURVIVAL_FACT_TYPES:
        return []
    violations: list[str] = []
    required_kill = {"switch_name", "action", "owner_id", "pair_id"}
    for row in rows:
        ft = str(row.get("fact_type") or "")
        if ft not in PHASE1_SURVIVAL_FACT_TYPES:
            continue
        payload = row.get("payload") or {}
        if ft == "kill_switch_triggered":
            missing = sorted(k for k in required_kill if not payload.get(k))
            if missing:
                violations.append(f"{ft}:missing:{','.join(missing)}")
    return violations


def _order_policy_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    types_used: set[str] = set()
    fak_retry_count = 0
    managed_rest_used = False
    resting_order_cancelled = False
    policy_mode: str | None = None
    open_resting_at_terminal = False

    terminal_idx: int | None = None
    for i, row in enumerate(rows):
        ft = str(row.get("fact_type") or "")
        if ft in {"paired_binary_done", "paired_binary_loop_stopped"}:
            terminal_idx = i
        payload = row.get("payload") or {}
        if ft == "survival_exit_order_type_selected":
            policy_mode = policy_mode or payload.get("policy_mode")
            ot = payload.get("order_type")
            if ot:
                types_used.add(str(ot))
        if ft == "survival_exit_order_repriced":
            fak_retry_count += 1
        if ft == "survival_exit_resting_order_placed":
            managed_rest_used = True
        if ft == "survival_exit_resting_order_cancelled":
            resting_order_cancelled = True

    if terminal_idx is not None:
        open_ids: set[str] = set()
        for i, row in enumerate(rows):
            if i > terminal_idx:
                break
            ft = str(row.get("fact_type") or "")
            payload = row.get("payload") or {}
            if ft == "survival_exit_resting_order_placed":
                oid = payload.get("order_id")
                if oid:
                    open_ids.add(str(oid))
            if ft == "survival_exit_resting_order_cancelled":
                oid = payload.get("order_id")
                if oid and str(oid) in open_ids:
                    open_ids.discard(str(oid))
        if open_ids:
            open_resting_at_terminal = True

    return {
        "survival_exit_order_policy_mode": policy_mode,
        "survival_exit_order_types_used": sorted(types_used),
        "fak_retry_count": fak_retry_count,
        "managed_rest_used": managed_rest_used,
        "resting_order_cancelled": resting_order_cancelled,
        "stale_order_left_open": open_resting_at_terminal,
    }


def _order_policy_safety_fail(rows: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    violations: list[str] = []
    metrics = _order_policy_report(rows)
    if metrics.get("stale_order_left_open"):
        violations.append("gtc_gtd_left_open_after_terminal")

    resting_ids: list[str] = []
    for row in rows:
        if row.get("fact_type") != "survival_exit_resting_order_placed":
            continue
        payload = row.get("payload") or {}
        oid = payload.get("order_id")
        if oid:
            resting_ids.append(str(oid))
    if len(resting_ids) != len(set(resting_ids)):
        violations.append("duplicate_resting_survival_orders")

    for row in rows:
        if row.get("fact_type") not in {
            "survival_exit_order_type_selected",
            "survival_exit_resting_order_placed",
        }:
            continue
        payload = row.get("payload") or {}
        if payload.get("post_only"):
            violations.append("post_only_survival_exit")
            break

    disable_near_close_s = 30.0
    for row in rows:
        if row.get("fact_type") != "survival_exit_resting_order_placed":
            continue
        payload = row.get("payload") or {}
        ttc = payload.get("time_to_close")
        if ttc is not None:
            try:
                if float(ttc) <= disable_near_close_s:
                    violations.append("managed_rest_near_close")
                    break
            except (TypeError, ValueError):
                pass

    for row in rows:
        if row.get("fact_type") != "intent":
            continue
        payload = row.get("payload") or {}
        if not payload.get("survival_exit"):
            continue
        corr = row.get("correlation_id")
        has_risk = any(
            r.get("fact_type") == "risk" and r.get("correlation_id") == corr for r in rows
        )
        if not has_risk:
            violations.append("survival_exit_bypassed_risk_path")
            break

    return bool(violations), violations


def _quality_reject_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    skips = [
        r
        for r in rows
        if r.get("fact_type") == "survival_enforce_exit_skipped"
        and (r.get("payload") or {}).get("skip_reason") == "quality_reject"
    ]
    retries = [
        r for r in rows if r.get("fact_type") == "survival_enforce_exit_retry_attempted"
    ]
    scheduled = [
        r for r in rows if r.get("fact_type") == "survival_enforce_exit_retry_scheduled"
    ]
    abandoned = [
        r
        for r in rows
        if r.get("fact_type") == "survival_enforce_exit_abandoned"
    ]
    pre_close_preempted = any(
        (r.get("payload") or {}).get("reason") == "pre_close_flatten_preempted" for r in abandoned
    )
    details: list[dict[str, Any]] = []
    for row in skips:
        payload = row.get("payload") or {}
        detail = payload.get("quality_reject_detail")
        if detail:
            details.append(detail)
    for row in retries:
        payload = row.get("payload") or {}
        detail = payload.get("quality_reject_detail")
        if detail and detail not in details:
            details.append(detail)
    return {
        "quality_reject_count": len(skips),
        "quality_reject_retry_count": len(retries),
        "pending_exit_intent_latched": bool(scheduled),
        "quality_reject_details": details,
        "pre_close_preempted_survival_exit": pre_close_preempted,
    }


def _trailing_quality_reject_pending_fail(rows: list[dict[str, Any]]) -> bool:
    types = _fact_types(rows)
    if "survivor_trailing_stop_triggered" not in types:
        return False
    metrics = _quality_reject_metrics(rows)
    if metrics["quality_reject_count"] == 0:
        return False
    trailing_submits = [
        r
        for r in rows
        if r.get("fact_type") == "survival_enforce_exit_submitted"
        and (r.get("payload") or {}).get("module") == "trailing_stop"
    ]
    if trailing_submits:
        return False
    retry_passed = any(
        (r.get("payload") or {}).get("retry_outcome") == "quality_passed"
        for r in rows
        if r.get("fact_type") == "survival_enforce_exit_retry_attempted"
    )
    if retry_passed:
        return False
    loop = _loop_stopped(rows)
    final_state = str((loop or {}).get("final_state", "")).upper()
    if final_state in {"DONE", "IDLE"} and metrics["pre_close_preempted_survival_exit"]:
        return True
    if final_state not in {"DONE", "IDLE"}:
        return True
    return metrics["quality_reject_count"] > 0 and not trailing_submits


def _phase1_enforce_safety_fail(rows: list[dict[str, Any]]) -> bool:
    submits = [r for r in rows if r.get("fact_type") == "survival_enforce_exit_submitted"]
    trailing = [r for r in submits if (r.get("payload") or {}).get("module") == "trailing_stop"]
    if len(trailing) > 1:
        return True
    if len(submits) > 2:
        return True
    return False


def refine_phase1_classification(rows: list[dict[str, Any]], base: str | None) -> str | None:
    if base != PHASE1_SURVIVAL_PASS:
        return base
    op_fail, _ = _order_policy_safety_fail(rows)
    if op_fail:
        return PHASE1_ORDER_POLICY_SAFETY_FAIL
    if _phase1_enforce_safety_fail(rows):
        return PHASE1_ENFORCE_SAFETY_FAIL
    if _trailing_quality_reject_pending_fail(rows):
        return PHASE1_TRAILING_QUALITY_REJECT_PENDING_FAIL
    types = _fact_types(rows)
    if "survival_enforce_exit_submitted" in types:
        last = [r for r in rows if r.get("fact_type") == "survival_enforce_exit_submitted"][-1]
        module = (last.get("payload") or {}).get("module")
        if module == "trailing_stop":
            return PHASE1_TRAILING_ENFORCE_PASS
        if module == "stall_exit":
            return PHASE1_STALL_ENFORCE_PASS
    downgrade_req = any(
        (r.get("payload") or {}).get("action") == "downgrade"
        for r in rows
        if r.get("fact_type") == "survival_enforce_exit_requested"
    )
    if downgrade_req and "survivor_target_downgraded" in types:
        return PHASE1_STALL_ENFORCE_PASS
    if "survivor_target_downgraded" in types or "survivor_target_selected" in types:
        return PHASE1_TARGET_POLICY_PASS
    return PHASE1_ADVISORY_PASS


def classify_phase1_run(rows: list[dict[str, Any]], *, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = manifest or {}
    if not _survival_enabled(rows, manifest):
        return {
            "phase1_classification": None,
            "phase1_survival_enabled": False,
            "phase1_missing_requirements": [],
            "survivor_phase_reached": survivor_phase_reached(rows),
            "is_phase1_advisory_pass": False,
        }

    types = _fact_types(rows)
    survival_facts = [ft for ft in PHASE1_SURVIVAL_FACT_TYPES if ft in types] if PHASE1_SURVIVAL_FACT_TYPES else []
    survivor_reached = survivor_phase_reached(rows)

    if activation_unwind_failure_pattern(rows) or (
        not survivor_reached and "paired_binary_terminal_summary" in types
        and (_terminal_summary(rows) or {}).get("survivor_phase_reached") is False
    ):
        return {
            "phase1_classification": None,
            "phase1_survival_enabled": True,
            "phase1_survival_facts_present": survival_facts,
            "phase1_missing_requirements": [],
            "runtime_premature_exit_detected": False,
            "survivor_phase_reached": False,
            "survival_advisory_not_exercised": True,
            "survival_advisory_not_exercised_reason": "activation_failed_before_survivor",
            "is_phase1_advisory_pass": False,
        }

    missing: list[str] = []
    if not survival_facts and survivor_reached:
        missing.append("missing_survival_facts")

    premature, premature_detail = _detect_premature_runtime_exit(rows)
    if premature:
        return {
            "phase1_classification": PHASE1_RUNTIME_PREMATURE_EXIT,
            "phase1_survival_enabled": True,
            "phase1_survival_facts_present": survival_facts,
            "phase1_missing_requirements": premature_detail,
            "runtime_premature_exit_detected": True,
            "survivor_phase_reached": survivor_reached,
            "is_phase1_advisory_pass": False,
        }

    contract_violations = _survival_fact_contract_violations(rows)
    missing.extend(contract_violations)

    loop = _loop_stopped(rows)
    final_ok = loop is None or str(loop.get("final_state", "")).upper() in {"DONE", "IDLE"}
    if not final_ok:
        missing.append(f"final_state_not_accepted:{loop.get('final_state') if loop else 'unknown'}")

    if not survivor_reached:
        classification: str | None = None
    elif not survival_facts:
        classification = PHASE2_WS_FAIL
    elif missing:
        classification = PHASE2_WS_FAIL
    else:
        classification = PHASE1_SURVIVAL_PASS

    pnl_info = _pnl_reconciliation_status(rows)
    refined = refine_phase1_classification(rows, classification)
    order_policy = _order_policy_report(rows)
    op_fail, op_violations = _order_policy_safety_fail(rows)
    quality_metrics = _quality_reject_metrics(rows)
    manual_ui_pnl_required = pnl_info.get("pnl_status") not in ("final", None)
    return {
        "phase1_classification": refined,
        "phase1_base_classification": classification,
        "phase1_survival_enabled": True,
        "phase1_survival_facts_present": survival_facts,
        "phase1_missing_requirements": missing + op_violations,
        "runtime_premature_exit_detected": False,
        "survivor_phase_reached": survivor_reached,
        "survival_advisory_not_exercised": not survivor_reached,
        "survival_advisory_not_exercised_reason": (
            "survivor_phase_not_reached" if not survivor_reached else None
        ),
        "is_phase1_advisory_pass": refined in {PHASE1_ADVISORY_PASS, PHASE1_SURVIVAL_PASS, PHASE1_TARGET_POLICY_PASS},
        "pnl_status": pnl_info.get("pnl_status"),
        "pnl_blocker_for_enforcement": pnl_info.get("pnl_blocker_for_enforcement"),
        "manual_ui_pnl_required": manual_ui_pnl_required,
        "phase1_order_policy_safety_fail": op_fail,
        **order_policy,
        **quality_metrics,
    }


def classify_phase2_run(rows: list[dict[str, Any]]) -> dict[str, Any]:
    force_ok, force_missing = _force_flatten_pass(rows)
    normal_ok, normal_missing = _normal_lifecycle_pass(rows)
    no_entry_ok, no_entry_missing = _no_entry_observation_pass(rows)
    pnl_computed, pnl_detail = _pnl_reported(rows)
    pnl_info = _pnl_reconciliation_status(rows)
    loop = _loop_stopped(rows)
    types = _fact_types(rows)
    has_force_flatten_attempt = FORCE_FLATTEN_FACTS[0] in types
    no_entry_summary = _no_entry_summary(rows)
    terminal = _terminal_summary(rows)
    activation_unwind_fail = activation_unwind_failure_pattern(rows)
    activation_missing = _activation_unwind_fail_diagnostics(rows) if activation_unwind_fail else []

    common_diag = {
        "survivor_phase_reached": survivor_phase_reached(rows),
        "is_phase1_advisory_pass": False,
        "hardening_metadata_ok": _hardening_metadata_ok(rows),
        "lifecycle_clock_known": _event_end_ts(rows) is not None,
        "one_shot_shutdown_observed": _one_shot_shutdown_observed(rows),
        "terminal_summary": terminal,
    }

    if activation_unwind_fail:
        classification = PHASE2_WS_ACTIVATION_UNWIND_FAIL
        lifecycle_complete = False
        force_flatten_complete = False
        missing = activation_missing
    elif _manual_intervention_shutdown(rows):
        classification = PHASE2_WS_FAIL
        lifecycle_complete = False
        force_flatten_complete = False
        missing = force_missing or normal_missing
    elif force_ok:
        classification = PHASE2_WS_OPEN_EXPOSURE_FORCE_FLATTEN_PASS
        lifecycle_complete = True
        force_flatten_complete = True
        missing = []
    elif normal_ok:
        classification = PHASE2_WS_LIFECYCLE_PASS
        lifecycle_complete = True
        force_flatten_complete = False
        missing = []
    elif has_force_flatten_attempt:
        classification = PHASE2_WS_FAIL
        lifecycle_complete = False
        force_flatten_complete = False
        missing = force_missing
    elif no_entry_ok:
        classification = PHASE2_WS_OBSERVATION_PASS
        lifecycle_complete = False
        force_flatten_complete = False
        missing = []
    elif no_entry_summary is not None:
        classification = PHASE2_WS_FAIL
        lifecycle_complete = False
        force_flatten_complete = False
        missing = no_entry_missing
    elif loop and str(loop.get("final_state", "")).upper() == "DONE":
        classification = PHASE2_WS_OBSERVATION_PASS
        lifecycle_complete = False
        force_flatten_complete = False
        missing = force_missing or normal_missing
    else:
        classification = PHASE2_WS_FAIL
        lifecycle_complete = False
        force_flatten_complete = False
        missing = force_missing or normal_missing

    result = {
        "classification": classification,
        "lifecycle_complete": lifecycle_complete,
        "force_flatten_complete": force_flatten_complete,
        "pnl_computed": pnl_computed,
        "pnl_detail": pnl_detail,
        "pnl_status": pnl_info.get("pnl_status"),
        "pnl_final": pnl_info.get("pnl_final"),
        "pnl_blocker_for_enforcement": pnl_info.get("pnl_blocker_for_enforcement"),
        "pnl_has_discrepancy": pnl_info.get("has_discrepancy"),
        "manual_ui_pnl_required": pnl_info.get("pnl_status") not in ("final", None),
        "missing_requirements": missing,
        "normal_lifecycle_missing": normal_missing,
        "force_flatten_missing": force_missing,
        "no_entry_observation_missing": no_entry_missing,
        "no_entry_summary": no_entry_summary,
        **common_diag,
    }
    if activation_unwind_fail:
        result["survival_advisory_not_exercised"] = True
        result["survival_advisory_not_exercised_reason"] = "activation_failed_before_survivor"
    return result


def analyze_phase2(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir if run_dir.is_absolute() else REPO / run_dir
    m8 = analyze_m8(run_dir)
    rows = _load_rows(run_dir / "facts.jsonl")
    manifest = _load_manifest(run_dir)
    phase1 = classify_phase1_run(rows, manifest=manifest)
    phase2 = classify_phase2_run(rows)
    summary = {**m8, **phase2, **phase1}
    if phase1.get("phase1_survival_enabled") and phase1.get("phase1_classification"):
        summary["classification"] = phase1.get("phase1_classification") or phase2["classification"]
    elif phase2.get("classification") == PHASE2_WS_ACTIVATION_UNWIND_FAIL:
        summary["classification"] = PHASE2_WS_ACTIVATION_UNWIND_FAIL
    else:
        summary["classification"] = phase2.get("classification")
    summary["recommendation"] = summary.get("classification")
    if summary.get("classification") in {PHASE2_WS_FAIL, PHASE2_WS_ACTIVATION_UNWIND_FAIL}:
        summary["blockers"] = list(
            dict.fromkeys(
                list(m8.get("blockers") or [])
                + phase2["missing_requirements"]
                + list(phase1.get("phase1_missing_requirements") or [])
            )
        )
    elif phase2["classification"] == PHASE2_WS_OPEN_EXPOSURE_FORCE_FLATTEN_PASS:
        summary["blockers"] = [
            b for b in (m8.get("blockers") or []) if "lifecycle" not in str(b).lower()
        ]
    return summary


def _to_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Phase 2 paired-binary live validation",
        "",
        f"**Classification:** `{summary.get('classification')}`",
        f"**Lifecycle complete:** `{summary.get('lifecycle_complete')}`",
        f"**Force flatten complete:** `{summary.get('force_flatten_complete')}`",
        f"**PnL computed:** `{summary.get('pnl_computed')}` (`{summary.get('pnl_detail')}`)",
        f"**PnL status:** `{summary.get('pnl_status')}`",
        f"**PnL final:** `{summary.get('pnl_final')}`",
        f"**Enforcement blocker (PnL):** `{summary.get('pnl_blocker_for_enforcement')}`",
        "",
    ]
    no_entry = summary.get("no_entry_summary")
    if no_entry:
        lines.extend(["## No-entry summary", "```json", json.dumps(no_entry, indent=2), "```", ""])
    if summary.get("missing_requirements"):
        lines.extend(["## Missing requirements", *[f"- {m}" for m in summary["missing_requirements"]], ""])
    if summary.get("blockers"):
        lines.extend(["## Blockers", *[f"- {b}" for b in summary["blockers"]], ""])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Phase 2 WS-primary paired-binary live run")
    parser.add_argument("run_dir", type=Path, help="Path to run directory with facts.jsonl")
    parser.add_argument("--json-out", type=Path, help="Write JSON summary")
    parser.add_argument("--md-out", type=Path, help="Write markdown summary")
    args = parser.parse_args()

    summary = analyze_phase2(args.run_dir)
    text = json.dumps(summary, indent=2)
    print(text)
    if args.json_out:
        args.json_out.write_text(text, encoding="utf-8")
    if args.md_out:
        args.md_out.write_text(_to_markdown(summary), encoding="utf-8")
    ok = summary.get("classification") in {
        PHASE2_WS_LIFECYCLE_PASS,
        PHASE2_WS_OPEN_EXPOSURE_FORCE_FLATTEN_PASS,
        PHASE2_WS_OBSERVATION_PASS,
        PHASE1_SURVIVAL_PASS,
    }
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
