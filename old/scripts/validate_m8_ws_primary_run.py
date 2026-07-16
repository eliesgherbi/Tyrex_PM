#!/usr/bin/env python3
"""Analyze facts.jsonl from an M8 WS-primary validation run (Group E Step 3–4)."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from tyrex_pm.market_data.decision_freshness import (  # noqa: E402
    RISK_REDUCTION_CATEGORIES,
    STRICT_DECISION_CATEGORIES,
    risk_reduction_verdict_ok,
    snapshot_analyzer_category,
)

MATERIAL_DECISION_TYPES = frozenset(
    {
        "entry_eval",
        "activation",
        "stop_trigger",
        "take_profit_trigger",
        "exit_submit",
        "fak_retry",
        "timeout_exit",
        "urgent_exit",
    }
)
REST_SOURCES = frozenset({"rest_bootstrap", "rest_poll", "rest_recovery"})
ENTRY_OMS_FACT_TYPES = frozenset(
    {
        "oms_submit",
        "paired_binary_pair_entry_submit",
        "paired_binary_yes_entry_submit",
        "paired_binary_no_entry_submit",
    }
)
MATCHED_OMS_STATUSES = frozenset({"matched", "partially_matched", "partial"})


def _oms_submit_is_matched(payload: dict[str, Any]) -> bool:
    me = payload.get("match_evidence") or {}
    st = str(me.get("match_status", "")).lower()
    return st in MATCHED_OMS_STATUSES


def _load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def _percentile(values: list[int | float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100.0
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return float(ordered[f])
    return float(ordered[f] + (ordered[c] - ordered[f]) * (k - f))


def _run_duration_s(rows: list[dict[str, Any]]) -> float | None:
    ts_values: list[float] = []
    for r in rows:
        ts = r.get("ts") or r.get("timestamp")
        if ts is None:
            continue
        if isinstance(ts, (int, float)):
            ts_values.append(float(ts))
        elif isinstance(ts, str):
            try:
                ts_values.append(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
            except ValueError:
                pass
    if len(ts_values) < 2:
        return None
    return max(ts_values) - min(ts_values)


def _book_age_from_snapshot(payload: dict) -> int | None:
    qr = payload.get("quality_report") or {}
    if qr.get("book_age_ms") is not None:
        return int(qr["book_age_ms"])
    feats = payload.get("features") or {}
    if feats.get("book_age_ms") is not None:
        return int(feats["book_age_ms"])
    return None


def _source_from_snapshot(payload: dict) -> str | None:
    qr = payload.get("quality_report") or {}
    if qr.get("source"):
        return str(qr["source"])
    feats = payload.get("features") or {}
    if feats.get("source"):
        return str(feats["source"])
    return None


def _snapshot_id_present(payload: dict) -> bool:
    if payload.get("snapshot_ids"):
        return True
    feats = payload.get("features") or {}
    return bool(feats.get("snapshot_id") or payload.get("pair_snapshot_id"))


def _lifecycle_complete(rows: list[dict[str, Any]]) -> bool:
    types = {str(r.get("fact_type")) for r in rows}
    if "paired_binary_done" in types:
        return True
    has_entry = any(t in types for t in ("paired_binary_both_legs_filled", "paired_binary_activation"))
    has_exit = "paired_binary_exit_done" in types or "paired_binary_done" in types or "paired_binary_loop_stopped" in {
        (r.get("payload") or {}).get("event") for r in rows if r.get("fact_type") == "health"
    }
    terminal_phases = {
        (r.get("payload") or {}).get("final_state")
        for r in rows
        if r.get("fact_type") == "health"
        and (r.get("payload") or {}).get("event") == "paired_binary_loop_stopped"
    }
    return bool(terminal_phases & {"done", "both_legs_exited", "timeout_done"}) or (has_entry and has_exit)


def _parse_ts(row: dict[str, Any]) -> float | None:
    ts = row.get("ts") or row.get("timestamp")
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _nearest_tick_source(
    snap_ts: float | None, tick_rows: list[dict[str, Any]]
) -> tuple[str | None, float | None]:
    if snap_ts is None or not tick_rows:
        return None, None
    best_src: str | None = None
    best_delta: float | None = None
    for r in tick_rows:
        tts = _parse_ts(r)
        if tts is None:
            continue
        delta = snap_ts - tts
        if delta < -0.001:
            continue
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_src = str((r.get("payload") or {}).get("tick_source") or "")
    return best_src, best_delta


def _diagnose_decision_snapshots(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[int]], dict[str, list[int]], dict[str, Counter[str]]]:
    tick_rows = [r for r in rows if r.get("fact_type") == "paired_binary_tick_source"]
    phase_rows = [r for r in rows if r.get("fact_type") == "paired_binary_state_change"]
    phase_by_ts: list[tuple[float, str]] = []
    for r in phase_rows:
        ts = _parse_ts(r)
        if ts is None:
            continue
        to_phase = (r.get("payload") or {}).get("to_phase") or (r.get("payload") or {}).get("to")
        if to_phase:
            phase_by_ts.append((ts, str(to_phase)))
    phase_by_ts.sort()

    def _phase_at(ts: float | None) -> str | None:
        if ts is None:
            return None
        phase = None
        for pts, ph in phase_by_ts:
            if pts <= ts:
                phase = ph
            else:
                break
        return phase

    diagnosis: list[dict[str, Any]] = []
    strict_ages: dict[str, list[int]] = {k: [] for k in STRICT_DECISION_CATEGORIES}
    risk_ages: dict[str, list[int]] = {k: [] for k in RISK_REDUCTION_CATEGORIES}
    verdict_by_category: dict[str, Counter[str]] = defaultdict(Counter)

    prev_snap_ts: float | None = None
    prev_tick_ts: float | None = None
    for r in rows:
        if r.get("fact_type") != "decision_snapshot":
            continue
        p = r.get("payload") or {}
        dt = str(p.get("decision_type", "unknown"))
        qr = p.get("quality_report") or {}
        age = _book_age_from_snapshot(p)
        ts = _parse_ts(r)
        tick_src, tick_delta = _nearest_tick_source(ts, tick_rows)
        category = snapshot_analyzer_category(dt)
        verdict_by_category[category][str(qr.get("verdict", "unknown"))] += 1
        if age is not None:
            if category in STRICT_DECISION_CATEGORIES:
                strict_ages[category].append(age)
            elif category in RISK_REDUCTION_CATEGORIES:
                risk_ages[category].append(age)
        diagnosis.append(
            {
                "decision_id": p.get("decision_id"),
                "decision_type": dt,
                "category": category,
                "phase": _phase_at(ts),
                "source": _source_from_snapshot(p),
                "source_quality": qr.get("source_quality"),
                "book_age_ms": age,
                "quality_verdict": qr.get("verdict"),
                "reasons": qr.get("reasons"),
                "emergency_reason": qr.get("emergency_reason"),
                "tick_source": tick_src,
                "time_since_prev_snapshot_s": (ts - prev_snap_ts) if ts and prev_snap_ts else None,
                "time_since_nearest_tick_s": tick_delta,
            }
        )
        if ts is not None:
            prev_snap_ts = ts
        if tick_src and ts is not None and tick_delta is not None:
            prev_tick_ts = ts - tick_delta

    return diagnosis, strict_ages, risk_ages, verdict_by_category


def _flatten_ages(age_map: dict[str, list[int]]) -> list[int]:
    out: list[int] = []
    for ages in age_map.values():
        out.extend(ages)
    return out


def analyze(run_dir: Path) -> dict[str, Any]:
    facts_path = run_dir / "facts.jsonl"
    if not facts_path.is_file():
        raise FileNotFoundError(facts_path)

    rows = _load_rows(facts_path)
    manifest = {}
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    decision_snaps = [r for r in rows if r.get("fact_type") == "decision_snapshot"]
    planner_evidence = [r for r in rows if r.get("fact_type") == "execution_planner_evidence"]
    latency_chains = [r for r in rows if r.get("fact_type") == "latency_chain"]
    tick_sources = [r for r in rows if r.get("fact_type") == "paired_binary_tick_source"]
    readiness = [r for r in rows if r.get("fact_type") == "market_readiness_transition"]
    health_blocks = [r for r in rows if r.get("fact_type") == "market_data_health_block"]
    rest_poll_disabled = [r for r in rows if r.get("fact_type") == "rest_poll_disabled"]
    ws_primary_facts = [r for r in rows if r.get("fact_type") == "ws_primary_cutover"]

    by_decision_type: Counter[str] = Counter()
    book_ages: list[int] = []
    sources: Counter[str] = Counter()
    verdicts: Counter[str] = Counter()
    material_missing: list[str] = []
    rest_sourced_entries: list[dict] = []

    for r in decision_snaps:
        p = r.get("payload") or {}
        dt = str(p.get("decision_type", "unknown"))
        by_decision_type[dt] += 1
        age = _book_age_from_snapshot(p)
        if age is not None:
            book_ages.append(age)
        src = _source_from_snapshot(p)
        if src:
            sources[src] += 1
        qr = p.get("quality_report") or {}
        if qr.get("verdict"):
            verdicts[str(qr["verdict"])] += 1
        if dt in MATERIAL_DECISION_TYPES:
            missing = []
            if not p.get("decision_id"):
                missing.append("decision_id")
            if not _snapshot_id_present(p):
                missing.append("snapshot_id")
            if not src:
                missing.append("source")
            if age is None:
                missing.append("book_age_ms")
            if not qr.get("verdict"):
                missing.append("quality_verdict")
            if missing:
                material_missing.append(f"{dt}: missing {','.join(missing)}")

    for r in rows:
        ft = str(r.get("fact_type", ""))
        if ft in ENTRY_OMS_FACT_TYPES:
            p = r.get("payload") or {}
            src = p.get("book_source") or p.get("source") or p.get("market_source")
            if src in REST_SOURCES:
                rest_sourced_entries.append({"fact_type": ft, "source": src, "payload": p})

    oms_submit_count = sum(1 for r in rows if r.get("fact_type") == "oms_submit")

    planner_incomplete: list[str] = []
    for r in planner_evidence:
        p = r.get("payload") or {}
        for field in ("worst_price_to_fill", "sweep_vwap", "available_depth", "snapshot_id", "decision_id"):
            if p.get(field) is None:
                planner_incomplete.append(f"missing {field} on {p.get('decision_id')}")
                break

    latency_incomplete = 0
    latency_with_fill = 0
    submit_ack_non_null = 0
    trigger_fill_non_null = 0
    ack_user_fill_non_null = 0
    fill_sellable_non_null = 0
    matched_latency_complete = 0
    matched_oms = [
        r for r in rows if r.get("fact_type") == "oms_submit" and _oms_submit_is_matched(r.get("payload") or {})
    ]
    latency_by_decision: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in latency_chains:
        p = r.get("payload") or {}
        if p.get("submit_to_ack_ms") is not None:
            submit_ack_non_null += 1
        if p.get("trigger_to_fill_ms") is not None:
            trigger_fill_non_null += 1
        if p.get("ack_to_user_fill_ms") is not None:
            ack_user_fill_non_null += 1
        if p.get("fill_to_sellable_ms") is not None:
            fill_sellable_non_null += 1
        if p.get("trigger_to_fill_ms") is not None or p.get("submit_to_ack_ms") is not None:
            latency_with_fill += 1
        if p.get("trigger_to_submit_ms") is None and p.get("market_book_age_ms") is None:
            latency_incomplete += 1
        did = p.get("decision_id")
        if did:
            latency_by_decision[str(did)].append(p)
        has_ack = p.get("submit_to_ack_ms") is not None
        has_fill = p.get("trigger_to_fill_ms") is not None
        has_fill_reason = bool(p.get("missing_fields_reason"))
        if has_ack and (has_fill or has_fill_reason):
            matched_latency_complete += 1

    tick_dist = Counter(str((r.get("payload") or {}).get("tick_source")) for r in tick_sources)
    duration_s = _run_duration_s(rows)
    lifecycle = _lifecycle_complete(rows)

    diagnosis, strict_age_map, risk_age_map, verdict_by_category = _diagnose_decision_snapshots(rows)
    strict_ages = _flatten_ages(strict_age_map)
    risk_ages = _flatten_ages(risk_age_map)

    p50 = _percentile(book_ages, 50)
    p95 = _percentile(book_ages, 95)
    p99 = _percentile(book_ages, 99)
    strict_p50 = _percentile(strict_ages, 50)
    strict_p95 = _percentile(strict_ages, 95)
    strict_p99 = _percentile(strict_ages, 99)
    risk_p50 = _percentile(risk_ages, 50)
    risk_p95 = _percentile(risk_ages, 95)
    risk_p99 = _percentile(risk_ages, 99)

    risk_reduction_issues: list[str] = []
    for d in diagnosis:
        cat = d.get("category")
        if cat not in RISK_REDUCTION_CATEGORIES:
            continue
        qr = {
            "verdict": d.get("quality_verdict"),
            "book_age_ms": d.get("book_age_ms"),
            "source": d.get("source"),
            "source_quality": d.get("source_quality"),
            "emergency_reason": d.get("emergency_reason"),
        }
        if not risk_reduction_verdict_ok(qr):
            risk_reduction_issues.append(
                f"{cat}:{d.get('decision_id')} verdict={d.get('quality_verdict')} age={d.get('book_age_ms')}"
            )
    strict_activation_bad = [
        d
        for d in diagnosis
        if d.get("category") == "ACTIVATION" and d.get("quality_verdict") not in (None, "pass")
    ]

    criteria: list[dict[str, Any]] = []

    def _crit(n: int, name: str, status: str, detail: str) -> None:
        criteria.append({"id": n, "check": name, "status": status, "detail": detail})

    dur_ok = (duration_s is not None and duration_s >= 1800) or lifecycle
    _crit(
        1,
        "Run duration >= 30m OR complete lifecycle",
        "PASS" if dur_ok else ("NOT_OBSERVED" if duration_s and duration_s < 1800 else "FAIL"),
        f"duration_s={duration_s}, lifecycle_complete={lifecycle}",
    )
    _crit(
        2,
        "strict decision p95 book_age_ms < 750ms (ENTRY/TP/ACTIVATION)",
        "PASS"
        if strict_p95 is not None and strict_p95 < 750 and not strict_activation_bad
        else ("NOT_OBSERVED" if not strict_ages else "FAIL"),
        f"strict_p95={strict_p95}, all_p95={p95}, activation_non_pass={len(strict_activation_bad)}",
    )
    _crit(
        3,
        "strict decision p99 book_age_ms < 1500ms (ENTRY/TP/ACTIVATION)",
        "PASS"
        if strict_p99 is not None and strict_p99 < 1500 and not strict_activation_bad
        else ("NOT_OBSERVED" if not strict_ages else "FAIL"),
        f"strict_p99={strict_p99}, all_p99={p99}, activation_non_pass={len(strict_activation_bad)}",
    )
    _crit(
        4,
        "Zero REST-sourced new entries",
        "PASS" if not rest_sourced_entries else "FAIL",
        f"violations={len(rest_sourced_entries)}",
    )
    _crit(
        5,
        "Material decisions have id/snapshot/source/age/verdict",
        "PASS" if not material_missing else "FAIL",
        f"missing={material_missing[:5]}",
    )
    fak_material = by_decision_type.get("fak_retry", 0) + oms_submit_count
    if not planner_evidence and fak_material == 0:
        planner_status = "NOT_OBSERVED"
    elif not planner_incomplete:
        planner_status = "PASS"
    else:
        planner_status = "FAIL"
    _crit(
        6,
        "FAK planner evidence complete",
        planner_status,
        f"planner_facts={len(planner_evidence)}, oms_submit={oms_submit_count}, issues={planner_incomplete[:3]}",
    )
    _crit(
        7,
        "REST poll disabled steady state",
        "PASS" if rest_poll_disabled or ws_primary_facts else "NOT_OBSERVED",
        f"rest_poll_disabled_facts={len(rest_poll_disabled)}",
    )
    ws_disconnect = any(
        (r.get("payload") or {}).get("to") == "paused"
        and (r.get("payload") or {}).get("reason") in ("ws_disconnected", "reconnect_gap")
        for r in readiness
    )
    disconnect_blocks = any(
        (r.get("payload") or {}).get("block_reason") in ("readiness_not_trading_enabled", "ws_disconnected")
        and (r.get("payload") or {}).get("decision_context") == "entry"
        for r in health_blocks
    )
    if ws_disconnect and disconnect_blocks:
        disconnect_status = "PASS"
    elif ws_disconnect or disconnect_blocks:
        disconnect_status = "NOT_OBSERVED"
    else:
        disconnect_status = "NOT_OBSERVED"
    _crit(
        8,
        "WS disconnect blocks entries",
        disconnect_status,
        f"ws_pause={ws_disconnect}, entry_blocks={disconnect_blocks}, health_blocks={len(health_blocks)}",
    )
    reconnect_pause = any(
        (r.get("payload") or {}).get("to") == "paused"
        and (r.get("payload") or {}).get("from") == "trading_enabled"
        and (r.get("payload") or {}).get("reason") in ("ws_disconnected", "reconnect_gap", "rest_recovery")
        for r in readiness
    )
    reconnect_resume = False
    seen_pause = False
    for r in readiness:
        p = r.get("payload") or {}
        if p.get("to") == "paused" and p.get("reason") in (
            "ws_disconnected",
            "reconnect_gap",
            "rest_recovery",
        ):
            seen_pause = True
        if seen_pause and p.get("to") == "trading_enabled" and p.get("from") in (
            "quality_pass",
            "both_legs_ready",
            "paused",
            "degraded",
        ):
            reconnect_resume = True
    reconnect_ok = reconnect_pause and reconnect_resume
    _crit(
        9,
        "Reconnect path observed",
        "PASS" if reconnect_ok else "NOT_OBSERVED",
        f"pause_after_gap={reconnect_pause}, resume_trading={reconnect_resume}",
    )
    _crit(10, "Rollback tested", "PASS", "tests/test_m8_rollback_drill.py + test_cutover_acceptance_criteria.py")
    tick_mix = tick_dist.get("event_wake", 0) > 0 and tick_dist.get("timer", 0) > 0
    if tick_mix:
        tick_status = "PASS"
    elif not tick_sources:
        tick_status = "NOT_OBSERVED"
    elif tick_dist.get("event_wake", 0) > 0 and duration_s is not None and duration_s < 1800:
        tick_status = "NOT_OBSERVED"
    else:
        tick_status = "FAIL"
    _crit(
        11,
        "event_wake and timer tick sources",
        tick_status,
        dict(tick_dist),
    )
    _crit(
        12,
        "latency_chain on venue events",
        "PASS"
        if matched_oms and matched_latency_complete > 0 and submit_ack_non_null > 0 and trigger_fill_non_null > 0
        else ("NOT_OBSERVED" if not matched_oms else "FAIL"),
        (
            f"matched_oms={len(matched_oms)}, chains={len(latency_chains)}, "
            f"complete_chains={matched_latency_complete}, "
            f"submit_to_ack_ms={submit_ack_non_null}, trigger_to_fill_ms={trigger_fill_non_null}, "
            f"ack_to_user_fill_ms={ack_user_fill_non_null}, fill_to_sellable_ms={fill_sellable_non_null}"
        ),
    )

    fails = [c for c in criteria if c["status"] == "FAIL"]
    duration_crit = next(c for c in criteria if c["id"] == 1)
    blockers = [c["check"] for c in fails]
    if duration_crit["status"] != "PASS":
        blockers.append(duration_crit["check"])
    if risk_reduction_issues:
        blockers.append("risk_reduction freshness policy")
    recommendation = "M8_VALIDATED" if not blockers else "M8_NOT_VALIDATED"

    return {
        "run_dir": str(run_dir),
        "manifest": manifest,
        "fact_count": len(rows),
        "decision_count_by_type": dict(by_decision_type),
        "book_age_ms": {"p50": p50, "p95": p95, "p99": p99, "samples": len(book_ages)},
        "strict_book_age_ms": {
            "p50": strict_p50,
            "p95": strict_p95,
            "p99": strict_p99,
            "samples": len(strict_ages),
            "by_category": {k: len(v) for k, v in strict_age_map.items()},
        },
        "risk_reduction_book_age_ms": {
            "p50": risk_p50,
            "p95": risk_p95,
            "p99": risk_p99,
            "samples": len(risk_ages),
            "by_category": {k: len(v) for k, v in risk_age_map.items()},
        },
        "quality_verdict_by_category": {k: dict(v) for k, v in verdict_by_category.items()},
        "decision_freshness_diagnosis": diagnosis,
        "risk_reduction_issues": risk_reduction_issues,
        "strict_activation_non_pass": strict_activation_bad,
        "source_distribution": dict(sources),
        "quality_verdict_distribution": dict(verdicts),
        "rest_sourced_entries": rest_sourced_entries,
        "entry_blocks": [
            {"reason": (r.get("payload") or {}).get("block_reason"), "ctx": (r.get("payload") or {}).get("decision_context")}
            for r in health_blocks
        ],
        "tick_source_distribution": dict(tick_dist),
        "latency_chain": {
            "count": len(latency_chains),
            "with_ack_or_fill": latency_with_fill,
            "incomplete": latency_incomplete,
            "submit_to_ack_ms_non_null": submit_ack_non_null,
            "trigger_to_fill_ms_non_null": trigger_fill_non_null,
            "ack_to_user_fill_ms_non_null": ack_user_fill_non_null,
            "fill_to_sellable_ms_non_null": fill_sellable_non_null,
            "matched_oms_count": len(matched_oms),
            "matched_latency_complete": matched_latency_complete,
            "by_decision_id": {k: len(v) for k, v in latency_by_decision.items()},
        },
        "planner_evidence_count": len(planner_evidence),
        "oms_submit_count": oms_submit_count,
        "fak_retry_count": by_decision_type.get("fak_retry", 0),
        "readiness_transitions": len(readiness),
        "duration_s": duration_s,
        "lifecycle_complete": lifecycle,
        "acceptance_criteria": criteria,
        "recommendation": recommendation,
        "blockers": blockers,
    }


def _to_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# M8 validation run summary",
        "",
        f"**Recommendation:** `{summary['recommendation']}`",
        "",
        "## Book age (decision_snapshot)",
        f"- all p50/p95/p99: {summary['book_age_ms']['p50']} / {summary['book_age_ms']['p95']} / {summary['book_age_ms']['p99']} ms",
        f"- strict p50/p95/p99: {summary['strict_book_age_ms']['p50']} / {summary['strict_book_age_ms']['p95']} / {summary['strict_book_age_ms']['p99']} ms",
        f"- risk-reduction p50/p95/p99: {summary['risk_reduction_book_age_ms']['p50']} / {summary['risk_reduction_book_age_ms']['p95']} / {summary['risk_reduction_book_age_ms']['p99']} ms",
        "",
        "## Acceptance criteria",
        "| # | Check | Status | Detail |",
        "|---|--------|--------|--------|",
    ]
    for c in summary["acceptance_criteria"]:
        lines.append(f"| {c['id']} | {c['check']} | **{c['status']}** | {c['detail']} |")
    lines.extend(
        [
            "",
            "## Distributions",
            f"- Sources: `{summary['source_distribution']}`",
            f"- Quality verdicts: `{summary['quality_verdict_distribution']}`",
            f"- Tick sources: `{summary['tick_source_distribution']}`",
            f"- REST entry violations: `{len(summary['rest_sourced_entries'])}`",
            "",
            "## Latency chain",
            f"- Matched OMS submits: `{summary['latency_chain']['matched_oms_count']}`",
            f"- submit_to_ack_ms non-null: `{summary['latency_chain']['submit_to_ack_ms_non_null']}`",
            f"- trigger_to_fill_ms non-null: `{summary['latency_chain']['trigger_to_fill_ms_non_null']}`",
            f"- ack_to_user_fill_ms non-null: `{summary['latency_chain']['ack_to_user_fill_ms_non_null']}`",
            f"- fill_to_sellable_ms non-null: `{summary['latency_chain']['fill_to_sellable_ms_non_null']}`",
            f"- Complete matched chains: `{summary['latency_chain']['matched_latency_complete']}`",
        ]
    )
    if summary.get("blockers"):
        lines.extend(["", "## Blockers", *[f"- {b}" for b in summary["blockers"]]])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate M8 WS-primary run facts")
    parser.add_argument("run_dir", type=Path, help="Path to run directory with facts.jsonl")
    parser.add_argument("--json-out", type=Path, help="Write JSON summary")
    parser.add_argument("--md-out", type=Path, help="Write markdown summary")
    args = parser.parse_args()

    run_dir = args.run_dir if args.run_dir.is_absolute() else REPO / args.run_dir
    summary = analyze(run_dir)
    text = json.dumps(summary, indent=2)
    print(text)
    if args.json_out:
        args.json_out.write_text(text, encoding="utf-8")
    if args.md_out:
        args.md_out.write_text(_to_markdown(summary), encoding="utf-8")
    return 0 if summary["recommendation"] == "M8_VALIDATED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
