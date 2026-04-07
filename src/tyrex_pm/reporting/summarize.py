"""Build ``summary.json`` / ``summary.md`` from run artifacts (RPT-01+)."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from statistics import median
from typing import Any

from tyrex_pm.reporting.capital_observability import (
    parse_risk_capital_flags_from_config_json,
)
from tyrex_pm.reporting.summary_schema_v1 import SUMMARY_SCHEMA_VERSION, validate_summary
from tyrex_pm.reporting.taxonomy import guru_row_delta_reason


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


def _fill_latency_ms_stats(facts: list[dict[str, Any]]) -> tuple[float | None, int]:
    """First lifecycle ``ts_event_ns`` → first fill ``ts_event_ns`` per ``client_order_id``."""
    by_coid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in facts:
        coid = r.get("client_order_id")
        if isinstance(coid, str) and coid:
            by_coid[coid].append(r)
    samples_ms: list[float] = []
    for rows in by_coid.values():
        lifetimes = [
            r
            for r in rows
            if r.get("fact_type") == "order_lifecycle" and r.get("ts_event_ns") is not None
        ]
        fills = [
            r
            for r in rows
            if r.get("fact_type") == "fill" and r.get("ts_event_ns") is not None
        ]
        if not fills or not lifetimes:
            continue
        t0 = min(int(x["ts_event_ns"]) for x in lifetimes)
        t1 = min(int(x["ts_event_ns"]) for x in fills)
        if t1 >= t0:
            samples_ms.append((t1 - t0) / 1e6)
    if not samples_ms:
        return None, 0
    return float(median(samples_ms)), len(samples_ms)


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

    cfg_snap_row: dict[str, Any] | None = None
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
        if ft == "config_snapshot" and cfg_snap_row is None:
            cfg_snap_row = row

    cfg_capital = (
        parse_risk_capital_flags_from_config_json(str(cfg_snap_row.get("config_json") or "{}"))
        if cfg_snap_row is not None
        else {"capital_gate_enabled": None, "parse_ok": False}
    )

    acct_triggers: Counter[str] = Counter()
    for row in facts:
        if row.get("fact_type") != "account_snapshot":
            continue
        trig = row.get("snapshot_trigger")
        if trig is not None:
            acct_triggers[str(trig)] += 1

    venue_ib = sum(
        1
        for r in facts
        if r.get("fact_type") == "order_lifecycle"
        and str(r.get("status") or "") == "DENIED"
        and r.get("venue_insufficient_balance_likely") is True
    )

    last_risk_by_cid: dict[str, dict[str, Any]] = {}
    for r in facts:
        if r.get("fact_type") != "risk_decision":
            continue
        cid = r.get("correlation_id")
        if isinstance(cid, str) and cid:
            last_risk_by_cid[cid] = r
    submit_gate_off = 0
    for r in facts:
        if r.get("fact_type") != "execution_outcome":
            continue
        if str(r.get("outcome") or "") != "submit":
            continue
        cid = r.get("correlation_id")
        if not isinstance(cid, str) or not cid:
            continue
        lr = last_risk_by_cid.get(cid)
        if lr is not None and lr.get("capital_gate_enabled") is False:
            submit_gate_off += 1

    wallet_numeric_risk = sum(
        1
        for r in facts
        if r.get("fact_type") == "risk_decision"
        and r.get("wallet_collateral_numeric_known") is True
    )
    wallet_missing_risk = sum(
        1
        for r in facts
        if r.get("fact_type") == "risk_decision"
        and r.get("wallet_collateral_numeric_known") is not True
    )

    guru_rows: list[dict[str, Any]] = []
    for cid, fmap in sorted(by_corr.items()):
        sig = (fmap.get("guru_signal") or [{}])[0]
        dec = (fmap.get("strategy_decision") or [{}])[-1]
        sz = (fmap.get("sizing") or [{}])[-1]
        # Last risk row for this correlation in fact stream order (per-cid map); avoids stale
        # empty dict when by_corr's last risk_decision slice is missing or mis-ordered.
        rsk = last_risk_by_cid.get(cid) or (fmap.get("risk_decision") or [{}])[-1]
        intent = (fmap.get("execution_intent") or [{}])[-1]
        outcomes = fmap.get("execution_outcome") or []
        fills = fmap.get("fill") or []
        life = fmap.get("order_lifecycle") or []
        last_ex = outcomes[-1] if outcomes else {}
        last_outcome_v = last_ex.get("outcome")
        last_outcome = str(last_outcome_v) if last_outcome_v is not None else None
        last_ex_rc_v = last_ex.get("reason_code")
        last_ex_rc = str(last_ex_rc_v) if last_ex_rc_v is not None else None
        gate_raw = rsk.get("gate")
        guru_rows.append(
            {
                "correlation_id": cid,
                "guru_notional_usd": _safe_float(sig.get("guru_size_raw"))
                * _safe_float(sig.get("guru_price_raw")),
                "target_qty": sz.get("target_qty"),
                "strategy_branch": dec.get("branch"),
                "risk_allowed": rsk.get("allowed"),
                "delta_reason": guru_row_delta_reason(
                    risk_allowed=rsk.get("allowed"),
                    risk_reason_code=str(rsk.get("reason_code") or ""),
                    strategy_reason_code=str(dec.get("reason_code") or ""),
                    gate=str(gate_raw) if gate_raw else None,
                    last_execution_outcome=last_outcome,
                    last_execution_reason_code=last_ex_rc,
                ),
                "had_execution_intent": bool(intent.get("correlation_id")),
                "execution_outcome_count": len(outcomes),
                "fill_count": len(fills),
                "lifecycle_count": len(life),
                "last_execution_outcome": last_outcome,
                "capital_gate_enabled_at_risk": rsk.get("capital_gate_enabled"),
                "pre_venue_collateral_check_at_risk": rsk.get(
                    "pre_venue_collateral_check_active",
                ),
                "account_snapshot_seq_at_risk": rsk.get("account_snapshot_seq"),
                "py_clob_balance_usd_at_risk": rsk.get("py_clob_balance_usd"),
                "py_clob_allowance_usd_at_risk": rsk.get("py_clob_allowance_usd"),
                "balance_canonical_usd_at_risk": rsk.get("balance_canonical_usd"),
                "capital_canonical_balance_source_at_risk": rsk.get(
                    "capital_canonical_balance_source",
                ),
                "capital_balance_business_trusted_at_risk": rsk.get(
                    "capital_balance_business_trusted",
                ),
                "estimated_buy_headroom_usd_at_risk": rsk.get("estimated_buy_headroom_usd"),
                "portfolio_deploy_at_risk_eval": rsk.get("portfolio_deploy_at_eval")
                or rsk.get("e_portfolio_at_eval"),
                "intent_notional_usd_at_risk": rsk.get("intent_notional_usd"),
                "wallet_collateral_numeric_known_at_risk": rsk.get(
                    "wallet_collateral_numeric_known",
                ),
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

    delta_counts: Counter[str] = Counter(str(r["delta_reason"]) for r in guru_rows)
    decision_counts: Counter[str] = Counter(
        str(r.get("decision") or "")
        for _cid, fmap in by_corr.items()
        for r in fmap.get("strategy_decision") or []
    )
    outcome_hist: Counter[str] = Counter()
    recon_hist: Counter[str] = Counter()
    for r in facts:
        if r.get("fact_type") == "execution_outcome":
            o = r.get("outcome")
            if o is not None:
                outcome_hist[str(o)] += 1
        if r.get("fact_type") == "reconciliation":
            o = r.get("outcome")
            if o is not None:
                recon_hist[str(o)] += 1
    lifecycle_status: Counter[str] = Counter()
    for r in facts:
        if r.get("fact_type") == "order_lifecycle":
            st = r.get("status")
            if st is not None:
                lifecycle_status[str(st)] += 1

    med_fill_ms, fill_n = _fill_latency_ms_stats(facts)

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
            "delta_reason_counts": dict(delta_counts.most_common()),
            "strategy_decision_histogram": dict(decision_counts.most_common()),
            "skipped_notional_usd_approx": sum(
                r["guru_notional_usd"]
                for r in guru_rows
                if r["delta_reason"]
                in (
                    "min_follow_notional",
                    "min_follow_price_missing",
                    "min_order_notional",
                )
            ),
            "rows": guru_rows,
        },
        "execution_quality": {
            "order_lifecycle_count": by_type.get("order_lifecycle", 0),
            "fill_count": by_type.get("fill", 0),
            "execution_outcome_count": by_type.get("execution_outcome", 0),
            "execution_outcome_histogram": dict(outcome_hist.most_common()),
            "lifecycle_status_histogram": dict(lifecycle_status.most_common()),
            "reconciliation_outcome_histogram": dict(recon_hist.most_common()),
            "reconciliation_count": by_type.get("reconciliation", 0),
            "position_snapshot_count": by_type.get("position", 0),
            "median_time_to_first_fill_ms": med_fill_ms,
            "fill_latency_sample_count": fill_n,
            "notes": (
                "Risk allow and execution_outcome submit are not a successful copy: check "
                "execution_outcome_histogram (error vs skip vs submit), lifecycle/denied, and fills. "
                "execution_outcome may include stage=pre_submit_book|instrument_quantize|framework_submit. "
                "fill_latency uses order_lifecycle vs fill ts_event_ns per client_order_id."
            ),
        },
        "capital_deployment": {
            "account_snapshot_count": by_type.get("account_snapshot", 0),
            "account_snapshot_by_trigger": dict(acct_triggers.most_common()),
            "deployment_budget_count": by_type.get("deployment_budget", 0),
            "config_capital_gate_enabled": cfg_capital.get("capital_gate_enabled"),
            "config_capital_parse_ok": cfg_capital.get("parse_ok"),
            "venue_insufficient_balance_denials": venue_ib,
            "execution_submissions_while_capital_gate_off": submit_gate_off,
            "risk_decisions_with_wallet_balance_numeric": wallet_numeric_risk,
            "risk_decisions_without_wallet_balance_numeric": wallet_missing_risk,
            "capital_operational_meaning": {
                "risk_uses_deployment_budget": True,
                "py_clob_balance_is_wallet_collateral": True,
                "canonical_balance_priority": (
                    "balance_canonical_usd prefers nautilus_cash_account when extractable; "
                    "else normalized py_clob (integer strings = Polymarket 1e-6 USDC atoms). "
                    "estimated_buy_headroom_usd follows balance_canonical_usd."
                ),
                "capital_gate_off_means": (
                    "Approvals did not require pre-venue wallet/allowance checks; "
                    "venue may still deny for insufficient funds."
                ),
            },
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
    gvu = summary["guru_vs_us"]
    exq = summary["execution_quality"]
    lines = [
        f"# Run summary ({summary['run_id']})",
        "",
        f"- execution_path: {summary['run_overview'].get('execution_path')}",
        f"- facts: strategy_decision={summary['strategy_behavior']['strategy_decision_count']}, "
        f"risk allow/deny={summary['risk_impact']['risk_decision_allow_count']}/"
        f"{summary['risk_impact']['risk_decision_deny_count']}",
        f"- guru_vs_us: correlations={gvu['correlation_count']}, "
        f"top deltas: {list(gvu.get('delta_reason_counts', {}).items())[:5]}",
        f"- execution: outcomes={exq['execution_outcome_count']}, "
        f"lifecycle={exq['order_lifecycle_count']}, fills={exq['fill_count']}, "
        f"reconciliation={exq.get('reconciliation_count', 0)}, "
        f"positions={exq.get('position_snapshot_count', 0)}",
        f"- outcomes histogram: {exq.get('execution_outcome_histogram', {})}",
        f"- median_fill_latency_ms: {exq.get('median_time_to_first_fill_ms')} "
        f"(n={exq.get('fill_latency_sample_count', 0)})",
        f"- capital: account_snapshots={summary['capital_deployment'].get('account_snapshot_count')}, "
        f"triggers={summary['capital_deployment'].get('account_snapshot_by_trigger')}, "
        f"venue_balance_denials={summary['capital_deployment'].get('venue_insufficient_balance_denials')}, "
        f"submits_while_gate_off="
        f"{summary['capital_deployment'].get('execution_submissions_while_capital_gate_off')}",
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
