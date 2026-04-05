"""Build ``summary.json`` / ``summary.md`` from run artifacts (RPT-01+)."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tyrex_pm.reporting.summary_schema_v1 import SUMMARY_SCHEMA_VERSION, validate_summary
from tyrex_pm.reporting.taxonomy import to_delta_reason


def _iter_facts_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(o, dict):
                yield o


def _iter_facts_sqlite(db_path: Path) -> Iterator[dict[str, Any]]:
    if not db_path.is_file():
        return
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("SELECT payload_json FROM facts ORDER BY id")
        for (blob,) in cur:
            try:
                o = json.loads(blob)
            except json.JSONDecodeError:
                continue
            if isinstance(o, dict):
                yield o
    finally:
        conn.close()


def load_facts(run_dir: Path) -> tuple[list[dict[str, Any]], str]:
    """Prefer materialized SQLite when present; otherwise JSONL."""
    run_dir = run_dir.resolve()
    db = run_dir / "run.sqlite"
    jp = run_dir / "facts.jsonl"
    if db.is_file():
        return list(_iter_facts_sqlite(db)), "sqlite"
    return list(_iter_facts_jsonl(jp)), "jsonl"


def build_summary(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    manifest_path = run_dir / "manifest.json"
    man: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            man = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            man = {}

    facts, _src = load_facts(run_dir)
    run_id = str(man.get("run_id") or (facts[0].get("run_id") if facts else "unknown"))

    by_type: Counter[str] = Counter()
    by_corr: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    token_counts: Counter[str] = Counter()

    for row in facts:
        ft = str(row.get("fact_type") or "")
        by_type[ft] += 1
        cid = row.get("correlation_id")
        if isinstance(cid, str) and cid:
            by_corr[cid][ft].append(row)
        tid = row.get("token_id")
        if isinstance(tid, str) and tid and ft in (
            "guru_signal",
            "strategy_decision",
            "execution_intent",
            "execution_outcome",
        ):
            token_counts[tid] += 1

    guru_rows: list[dict[str, Any]] = []
    for cid, fmap in sorted(by_corr.items()):
        sig = (fmap.get("guru_signal") or [{}])[0]
        dec = (fmap.get("strategy_decision") or [{}])[-1]
        sz = (fmap.get("sizing") or [{}])[-1]
        rsk = (fmap.get("risk_decision") or [{}])[-1]
        intent = (fmap.get("execution_intent") or [{}])[-1]
        outcomes = fmap.get("execution_outcome") or []
        fills = fmap.get("fill") or []
        life = fmap.get("order_lifecycle") or []
        delta_rc = str(rsk.get("reason_code") or dec.get("reason_code") or "")
        gate = rsk.get("gate")
        gate_s = str(gate) if gate else None
        guru_rows.append(
            {
                "correlation_id": cid,
                "guru_notional_usd": _safe_float(sig.get("guru_size_raw"))
                * _safe_float(sig.get("guru_price_raw")),
                "target_qty": sz.get("target_qty"),
                "strategy_branch": dec.get("branch"),
                "risk_allowed": rsk.get("allowed"),
                "delta_reason": to_delta_reason(delta_rc, gate_s),
                "had_execution_intent": bool(intent.get("correlation_id")),
                "execution_outcome_count": len(outcomes),
                "fill_count": len(fills),
                "lifecycle_count": len(life),
            },
        )

    def _risk_n(*, allowed: bool) -> int:
        return sum(
            1
            for r in facts
            if r.get("fact_type") == "risk_decision" and r.get("allowed") is allowed
        )

    risk_allowed = _risk_n(allowed=True)
    risk_denied = _risk_n(allowed=False)

    pipeline = [r for r in facts if r.get("fact_type") == "report_pipeline_health"]
    pipeline_last = pipeline[-1] if pipeline else {}

    config_sha = ""
    for r in facts:
        if r.get("fact_type") == "config_snapshot":
            config_sha = str(r.get("config_sha256") or "")
            break

    anomalies: dict[str, Any] = {
        "duplicate_guru_signal_correlation_ids": man.get("data_quality", {}).get(
            "duplicate_guru_signal_correlation_ids",
        ),
        "fact_counts": dict(by_type),
    }

    dq = man.get("data_quality")
    if not isinstance(dq, dict):
        dq = {}

    summary: dict[str, Any] = {
        "summary_schema_version": SUMMARY_SCHEMA_VERSION,
        "run_id": run_id,
        "run_overview": {
            "run_dir": str(run_dir),
            "started_at_utc": man.get("started_at_utc"),
            "ended_at_utc": man.get("ended_at_utc"),
            "trader_id": man.get("trader_id"),
            "execution_path": man.get("execution_path"),
            "strategy_name": man.get("strategy_name"),
            "git_sha": man.get("git_sha"),
        },
        "strategy_behavior": {
            "strategy_decision_count": by_type.get("strategy_decision", 0),
            "sizing_count": by_type.get("sizing", 0),
        },
        "guru_vs_us": {
            "correlation_count": len(by_corr),
            "rows": guru_rows,
        },
        "execution_quality": {
            "order_lifecycle_count": by_type.get("order_lifecycle", 0),
            "fill_count": by_type.get("fill", 0),
            "execution_outcome_count": by_type.get("execution_outcome", 0),
            "median_time_to_first_fill_ms": None,
            "notes": "computed_when_lifecycle_timestamps_available",
        },
        "capital_deployment": {
            "account_snapshot_count": by_type.get("account_snapshot", 0),
            "exposure_count": by_type.get("exposure", 0),
        },
        "risk_impact": {
            "risk_decision_allow_count": risk_allowed,
            "risk_decision_deny_count": risk_denied,
        },
        "anomalies": anomalies,
        "token_breakdown": {
            "top_tokens_by_volume_events": [
                {"token_id": t, "events": n} for t, n in token_counts.most_common(32)
            ],
        },
        "config_fingerprint": {"config_sha256": config_sha},
        "pipeline_health": {
            "last_flush_ok": pipeline_last.get("flush_ok"),
            "facts_dropped": pipeline_last.get("facts_dropped"),
            "flush_errors": pipeline_last.get("flush_errors"),
            "queue_high_water": pipeline_last.get("queue_high_water"),
            "sink_stats": man.get("reporting_sink_stats"),
        },
        "data_quality_flags": dq,
    }
    validate_summary(summary)
    return summary


def _safe_float(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def write_summary_artifacts(run_dir: Path) -> tuple[Path, Path]:
    """Write ``summary.json`` and ``summary.md`` under ``run_dir``."""
    run_dir = run_dir.resolve()
    summary = build_summary(run_dir)
    js = run_dir / "summary.json"
    md = run_dir / "summary.md"
    js.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    lines = [
        f"# Run summary ({summary['run_id']})",
        "",
        f"- execution_path: {summary['run_overview'].get('execution_path')}",
        f"- facts: strategy_decision={summary['strategy_behavior']['strategy_decision_count']}, "
        f"risk allow/deny={summary['risk_impact']['risk_decision_allow_count']}/"
        f"{summary['risk_impact']['risk_decision_deny_count']}",
        f"- execution: outcomes={summary['execution_quality']['execution_outcome_count']}, "
        f"lifecycle={summary['execution_quality']['order_lifecycle_count']}, "
        f"fills={summary['execution_quality']['fill_count']}",
        "",
        "## data_quality_flags",
        "",
        "```",
        json.dumps(summary['data_quality_flags'], indent=2),
        "```",
        "",
    ]
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return js, md
