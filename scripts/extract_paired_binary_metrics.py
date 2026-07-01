#!/usr/bin/env python3
"""Extract paired-binary M9 comparison metrics from facts.jsonl (tolerant of older schemas)."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
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
    snapshot_analyzer_category,
)

REST_SOURCES = frozenset({"rest_bootstrap", "rest_poll", "rest_recovery"})
ENTRY_OMS_TYPES = frozenset({"oms_submit"})


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_prefix(path: Path) -> str | None:
    h = _sha256(path)
    return h[:16] if h else None


def _load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


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


def _book_age_from_snapshot(payload: dict) -> int | None:
    qr = payload.get("quality_report") or {}
    if qr.get("book_age_ms") is not None:
        return int(qr["book_age_ms"])
    feats = payload.get("features") or {}
    if feats.get("book_age_ms") is not None:
        return int(feats["book_age_ms"])
    return None


def _unavailable(value: Any) -> str | Any:
    return "UNAVAILABLE" if value is None else value


def _infer_backbone(rows: list[dict[str, Any]]) -> str:
    if any(r.get("fact_type") == "ws_primary_cutover" for r in rows):
        return "WS_PRIMARY"
    if any(r.get("fact_type") == "rest_poll_disabled" for r in rows):
        return "WS_PRIMARY"
    snaps = [r for r in rows if r.get("fact_type") == "decision_snapshot"]
    ws = sum(
        1
        for r in snaps
        if ((r.get("payload") or {}).get("quality_report") or {}).get("source_quality") == "ws_primary"
    )
    if ws > 0:
        return "WS_PRIMARY"
    return "REST_POLL"


def _oms_is_matched(payload: dict) -> bool:
    me = payload.get("match_evidence") or {}
    return str(me.get("match_status", "")).lower() in {"matched", "partially_matched", "partial"}


def extract_run(run_dir: Path, *, provenance: str = "VERIFIED") -> dict[str, Any]:
    facts_path = run_dir / "facts.jsonl"
    if not facts_path.is_file():
        raise FileNotFoundError(facts_path)

    rows = _load_rows(facts_path)
    manifest: dict[str, Any] = {}
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    run_summary: dict[str, Any] = {}
    summary_path = run_dir / "run_summary.json"
    if summary_path.is_file():
        run_summary = json.loads(summary_path.read_text(encoding="utf-8"))

    ts_values = [_parse_ts(r) for r in rows]
    ts_values = [t for t in ts_values if t is not None]
    duration_s = max(ts_values) - min(ts_values) if len(ts_values) >= 2 else None

    first_pb = next(
        (r.get("payload") or {} for r in rows if str(r.get("fact_type", "")).startswith("paired_binary")),
        {},
    )
    market_id = first_pb.get("market_id") or "UNAVAILABLE"

    decision_snaps = [r for r in rows if r.get("fact_type") == "decision_snapshot"]
    by_decision_type: Counter[str] = Counter()
    book_ages: list[int] = []
    entry_ages: list[int] = []
    activation_ages: list[int] = []
    exit_ages: list[int] = []
    sources: Counter[str] = Counter()
    verdicts: Counter[str] = Counter()

    for r in decision_snaps:
        p = r.get("payload") or {}
        dt = str(p.get("decision_type", "unknown"))
        by_decision_type[dt] += 1
        age = _book_age_from_snapshot(p)
        if age is not None:
            book_ages.append(age)
        qr = p.get("quality_report") or {}
        if qr.get("source"):
            sources[str(qr["source"])] += 1
        if qr.get("verdict"):
            verdicts[str(qr["verdict"])] += 1
        cat = snapshot_analyzer_category(dt)
        if age is not None:
            if cat == "ENTRY":
                entry_ages.append(age)
            elif cat == "ACTIVATION":
                activation_ages.append(age)
            elif cat in RISK_REDUCTION_CATEGORIES:
                exit_ages.append(age)

    activation_capture = [
        r for r in rows if r.get("fact_type") == "paired_binary_book_capture_quality"
        and (r.get("payload") or {}).get("event") == "activation"
    ]
    for r in activation_capture:
        p = r.get("payload") or {}
        ages = [p.get("yes_book_age_ms"), p.get("no_book_age_ms"), p.get("activation_book_age_ms")]
        for a in ages:
            if a is not None:
                activation_ages.append(int(a))

    latency_chains = [r for r in rows if r.get("fact_type") == "latency_chain"]
    latency_samples = [r for r in rows if r.get("fact_type") == "paired_binary_latency_sample"]
    lat_fields = [
        "trigger_to_submit_ms",
        "submit_to_ack_ms",
        "trigger_to_fill_ms",
        "ack_to_user_fill_ms",
        "fill_to_sellable_ms",
    ]
    lat_agg: dict[str, list[int]] = {f: [] for f in lat_fields}
    for r in latency_chains + latency_samples:
        p = r.get("payload") or {}
        for f in lat_fields:
            v = p.get(f)
            if isinstance(v, (int, float)):
                lat_agg[f].append(int(v))

    planner_rows = [r for r in rows if r.get("fact_type") == "execution_planner_evidence"]
    planner_incomplete = 0
    required_pe = ("worst_price_to_fill", "sweep_vwap", "available_depth", "snapshot_id", "decision_id")
    for r in planner_rows:
        p = r.get("payload") or {}
        if any(p.get(k) is None for k in required_pe):
            planner_incomplete += 1

    oms_submits = [r for r in rows if r.get("fact_type") == "oms_submit"]
    oms_rejects = [r for r in rows if r.get("fact_type") == "oms_reject"]
    fak_retry = sum(1 for r in rows if (r.get("payload") or {}).get("decision_type") == "fak_retry")
    rest_oms = sum(
        1
        for r in oms_submits
        if (r.get("payload") or {}).get("book_source") in REST_SOURCES
        or (r.get("payload") or {}).get("source") in REST_SOURCES
    )

    pnl_fact = next((r for r in rows if r.get("fact_type") == "paired_binary_realized_pnl"), None)
    pnl_unavail = any(r.get("fact_type") == "paired_binary_realized_pnl_unavailable" for r in rows)
    realized_pnl = None
    pnl_reason = "UNAVAILABLE"
    if pnl_fact:
        realized_pnl = (pnl_fact.get("payload") or {}).get("total_pnl_usd") or (
            pnl_fact.get("payload") or {}
        ).get("net_pnl_usd")
        pnl_reason = "paired_binary_realized_pnl"
    elif pnl_unavail:
        pnl_reason = "paired_binary_realized_pnl_unavailable"

    done = next((r for r in rows if r.get("fact_type") == "paired_binary_done"), None)
    run_outcome = "UNAVAILABLE"
    final_state = None
    if done:
        run_outcome = str((done.get("payload") or {}).get("state") or "DONE")
        final_state = (done.get("payload") or {}).get("state")
    elif any(r.get("fact_type") == "paired_binary_loop_stopped" for r in rows):
        run_outcome = "STOPPED"
    elif any((r.get("payload") or {}).get("final_state") == "FAILED" for r in rows if r.get("fact_type") == "health"):
        run_outcome = "FAILED"

    activation_abort = None
    for ft in (
        "paired_binary_pair_entry_manual_intervention",
        "paired_binary_activation_rejected_loss_budget",
        "paired_binary_pair_preflight_rejected",
    ):
        hit = next((r for r in rows if r.get("fact_type") == ft), None)
        if hit:
            activation_abort = (hit.get("payload") or {}).get("reason") or ft
            break

    survivor_timeout = any(
        (r.get("payload") or {}).get("event") == "paired_binary_loop_stopped"
        and (r.get("payload") or {}).get("reason") == "max_runtime_s"
        for r in rows
        if r.get("fact_type") == "health"
    ) or any(r.get("fact_type") == "paired_binary_timeout_exit" for r in rows)

    strategy_path = REPO / "config" / "strategies" / "paired_binary.yaml"
    scenario_hint = manifest.get("scenario") or manifest.get("scenario_file")

    return {
        "run_dir": str(run_dir),
        "run_name": manifest.get("run_name") or run_dir.name,
        "run_id": manifest.get("run_id") or "UNAVAILABLE",
        "provenance": provenance,
        "backbone": _infer_backbone(rows),
        "run_outcome": run_outcome,
        "final_state": _unavailable(final_state),
        "market": market_id,
        "duration_s": _unavailable(round(duration_s, 2) if duration_s is not None else None),
        "strategy_config_hash": _unavailable(_sha256_prefix(strategy_path)),
        "strategy_config_sha256": _unavailable(_sha256(strategy_path)),
        "scenario_config_hash": "UNAVAILABLE",
        "git_commit": manifest.get("git_sha") or "UNAVAILABLE",
        "execution_mode": manifest.get("execution_mode") or "UNAVAILABLE",
        "fact_count": len(rows),
        "decision_count_by_type": dict(by_decision_type),
        "source_distribution": dict(sources),
        "quality_verdict_distribution": dict(verdicts),
        "book_age_ms": {
            "p50": _unavailable(_percentile(book_ages, 50)),
            "p95": _unavailable(_percentile(book_ages, 95)),
            "p99": _unavailable(_percentile(book_ages, 99)),
            "samples": len(book_ages),
        },
        "entry_book_age_ms": {
            "p50": _unavailable(_percentile(entry_ages, 50)),
            "p95": _unavailable(_percentile(entry_ages, 95)),
            "samples": len(entry_ages),
        },
        "activation_book_age_ms": {
            "p50": _unavailable(_percentile(activation_ages, 50)),
            "p95": _unavailable(_percentile(activation_ages, 95)),
            "samples": len(activation_ages),
        },
        "exit_book_age_ms": {
            "p50": _unavailable(_percentile(exit_ages, 50)),
            "p95": _unavailable(_percentile(exit_ages, 95)),
            "samples": len(exit_ages),
        },
        "latency_ms": {
            f: {
                "non_null_count": len(v),
                "p50": _unavailable(_percentile(v, 50)),
                "p95": _unavailable(_percentile(v, 95)),
            }
            for f, v in lat_agg.items()
        },
        "planner_evidence_count": len(planner_rows),
        "planner_evidence_incomplete": planner_incomplete,
        "fak_reject_count": len(oms_rejects),
        "fak_retry_count": fak_retry,
        "oms_submit_count": len(oms_submits),
        "oms_matched_count": sum(1 for r in oms_submits if _oms_is_matched(r.get("payload") or {})),
        "rest_sourced_oms_submit_count": rest_oms,
        "realized_pnl_usd": _unavailable(realized_pnl),
        "pnl_source": pnl_reason,
        "survivor_timeout": survivor_timeout,
        "activation_abort_reason": _unavailable(activation_abort),
        "latency_chain_count": len(latency_chains),
        "run_summary": run_summary or None,
    }


def load_reconstructed(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract paired-binary metrics from a run directory")
    parser.add_argument("run_dir", type=Path, help="Path to run directory with facts.jsonl")
    parser.add_argument("--provenance", default="VERIFIED", help="VERIFIED | RECONSTRUCTED | SUBSTITUTED")
    parser.add_argument("--json-out", type=Path, help="Write JSON metrics file")
    args = parser.parse_args()

    run_dir = args.run_dir if args.run_dir.is_absolute() else REPO / args.run_dir
    metrics = extract_run(run_dir, provenance=args.provenance)
    text = json.dumps(metrics, indent=2)
    print(text)
    if args.json_out:
        out = args.json_out if args.json_out.is_absolute() else REPO / args.json_out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
