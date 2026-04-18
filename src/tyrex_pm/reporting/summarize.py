from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from tyrex_pm.reporting.oms_payload import get_oms_result_text
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_GURU_POLL,
    FACT_TYPE_GURU_SIGNAL,
    FACT_TYPE_INTENT,
    FACT_TYPE_OMS_SUBMIT,
    FACT_TYPE_RISK,
    FACT_TYPE_STRATEGY_SKIP,
)


def _load_fact_rows(facts_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not facts_path.is_file():
        return rows
    with facts_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def operator_hollow_run_view(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Explain 'hollow' runs (health-only or poll with zero downstream pipeline)."""
    counts = Counter(str(r.get("fact_type")) for r in rows)
    guru_polls = [r for r in rows if r.get("fact_type") == FACT_TYPE_GURU_POLL]
    last_poll = guru_polls[-1].get("payload") if guru_polls else None
    notes: list[str] = []

    if counts["health"] >= 2 and counts[FACT_TYPE_GURU_SIGNAL] == 0 and counts[FACT_TYPE_INTENT] == 0:
        if not guru_polls:
            notes.append(
                "Only health facts: run may have exited before any guru poll wrote facts "
                "(e.g. wrong mode), or this is a pre-guru_poll build."
            )
        elif last_poll and last_poll.get("source") == "data_api":
            if last_poll.get("new_signals", 0) == 0:
                if last_poll.get("raw_rows", 0) == 0:
                    notes.append(
                        "guru_poll: Data API returned zero raw activity rows for this wallet/page "
                        "(empty history or wrong address)."
                    )
                else:
                    notes.append(
                        "guru_poll: raw_rows>0 but new_signals=0 — all rows filtered by "
                        "dedup, watermark, or normalizer dropped them."
                    )
            if not last_poll.get("guru_wallet_configured", True):
                notes.append("guru_wallet is still placeholder — live/shadow poll is not meaningful until set.")
        elif last_poll and last_poll.get("source") == "fixture":
            if last_poll.get("new_signals_after_ingest", 0) == 0:
                notes.append(
                    "Fixture guru_poll: new_signals_after_ingest=0 — watermark/dedup consumed "
                    "all rows (reuse --state-dir or reset store)."
                )

    oms_submits = [r for r in rows if r.get("fact_type") == FACT_TYPE_OMS_SUBMIT]
    legacy_oms = sum(
        1
        for r in oms_submits
        if r.get("payload")
        and "shadow_result" in r["payload"]
        and "oms_result" not in r["payload"]
    )

    return {
        "operator_notes": notes,
        "guru_poll_count": len(guru_polls),
        "last_guru_poll": last_poll,
        "fact_counts_by_type": dict(counts),
        "legacy_shadow_result_only_oms_facts": legacy_oms,
    }


def audit_fact_joins(run_dir: Path, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """
    Parity join audit (T8): group facts by correlation_id and verify chains
    guru_signal → intent_created → risk_decision → oms_submit for approved paths.
    """
    if rows is None:
        rows = _load_fact_rows(run_dir / "facts.jsonl")
    by_corr: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_corr[r.get("correlation_id")].append(r)

    complete_chains = 0
    skipped = 0
    incomplete: list[dict[str, Any]] = []

    for corr, chain in by_corr.items():
        if corr is None:
            continue
        types = {r.get("fact_type") for r in chain}
        if FACT_TYPE_STRATEGY_SKIP in types:
            skipped += 1
            continue
        if FACT_TYPE_GURU_SIGNAL not in types:
            continue
        need = {FACT_TYPE_GURU_SIGNAL, FACT_TYPE_INTENT, FACT_TYPE_RISK, FACT_TYPE_OMS_SUBMIT}
        if need.issubset(types):
            risk = next(r for r in chain if r.get("fact_type") == FACT_TYPE_RISK)
            if risk.get("payload", {}).get("approved"):
                complete_chains += 1
            else:
                incomplete.append({"correlation_id": corr, "reason": "risk_not_approved"})
        elif FACT_TYPE_GURU_SIGNAL in types:
            incomplete.append({"correlation_id": corr, "reason": "missing_downstream", "had": sorted(types)})

    return {
        "correlation_groups": len([k for k in by_corr if k is not None]),
        "complete_approved_chains": complete_chains,
        "strategy_skips": skipped,
        "incomplete": incomplete,
    }


def summarize_run(run_dir: Path) -> dict[str, Any]:
    facts_path = run_dir / "facts.jsonl"
    rows = _load_fact_rows(facts_path)
    counts: Counter[str] = Counter()
    risk_reasons: Counter[str] = Counter()
    oms_results_sample: list[str | None] = []

    if not facts_path.is_file():
        return {
            "facts": 0,
            "by_type": {},
            "top_risk_reasons": [],
            "join_audit": audit_fact_joins(run_dir, []),
            "operator_view": operator_hollow_run_view([]),
            "oms_submit_canonical_key": "oms_result",
        }

    for row in rows:
        ft = row.get("fact_type", "?")
        counts[ft] += 1
        if ft == "risk_decision":
            for c in row.get("payload", {}).get("reason_codes", []) or []:
                risk_reasons[str(c)] += 1
        if ft == FACT_TYPE_OMS_SUBMIT:
            oms_results_sample.append(get_oms_result_text(row.get("payload")))

    summary_path = run_dir / "run_summary.json"
    run_summary_on_disk: dict[str, Any] | None = None
    if summary_path.is_file():
        try:
            run_summary_on_disk = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            run_summary_on_disk = None

    return {
        "facts": sum(counts.values()),
        "by_type": dict(counts),
        "top_risk_reasons": risk_reasons.most_common(20),
        "join_audit": audit_fact_joins(run_dir, rows),
        "operator_view": operator_hollow_run_view(rows),
        "run_summary_json": run_summary_on_disk,
        "oms_submit_canonical_key": "oms_result",
        "oms_result_samples_tail": [x for x in oms_results_sample[-5:] if x is not None],
    }
