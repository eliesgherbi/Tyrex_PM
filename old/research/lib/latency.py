"""Latency prior derivation from live run facts (M2B.4; no strategy imports).

The latency prior gates strict Notebook 02 protection-distance candidates. When no
``var/runs/**/facts.jsonl`` files exist, the prior is empty and Notebook 02 correctly
emits ``insufficient`` for buffer candidates.

Partial extraction: older runs without M2B.0-C ``latency_chain`` may still expose
``submit_to_ack`` by pairing ``oms_submit`` with ``oms_result`` facts on the same
``client_order_id`` / ``venue_order_id``, or from explicit ``submit_to_ack_ms`` fields.

Operator action to unlock full prior:
  - Copy existing live run directories with facts.jsonl into ``var/runs/``, or
  - Execute one short validation run that emits M2B.0-C correlation fields.

Do not fabricate ``event_to_decision`` or ``total_trigger_to_ack`` when segments are missing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

LATENCY_CHAIN_FACT = "latency_chain"
CORRELATION_FACTS = ("decision_snapshot", "latency_chain", "paired_binary_tick_source")
OMS_SUBMIT_FACT = "oms_submit"
OMS_RESULT_FACT = "oms_result"
LATENCY_SAMPLE_FACT = "paired_binary_latency_sample"


def _percentiles(series: pd.Series) -> dict[str, float | None]:
    if series.empty:
        return {"p50": None, "p90": None, "p95": None}
    return {
        "p50": float(series.quantile(0.50)),
        "p90": float(series.quantile(0.90)),
        "p95": float(series.quantile(0.95)),
    }


def _read_facts_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _extract_ms(payload: dict[str, Any], key: str) -> float | None:
    val = payload.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _event_to_decision_ms(payload: dict[str, Any]) -> float | None:
    """From M2B.0-C correlation: event_recv_ts vs decision_wall_ts when ISO strings present."""
    recv = payload.get("event_recv_ts")
    wall = payload.get("decision_wall_ts")
    if not recv or not wall:
        return None
    try:
        t0 = pd.to_datetime(recv, utc=True)
        t1 = pd.to_datetime(wall, utc=True)
        return float((t1 - t0).total_seconds() * 1000.0)
    except Exception:
        return None


def _ts_ms(ts: Any) -> float | None:
    if ts is None:
        return None
    try:
        t = pd.to_datetime(ts, utc=True)
        return float(t.timestamp() * 1000.0)
    except Exception:
        return None


def _order_key(payload: dict[str, Any]) -> str | None:
    for key in ("client_order_id", "venue_order_id", "order_id", "local_order_id"):
        val = payload.get(key)
        if val:
            return str(val)
    return None


def _extract_partial_submit_to_ack(rows: list[dict[str, Any]]) -> list[float]:
    """Pair oms_submit → oms_result timestamps for submit→ack when explicit ms missing."""
    submits: dict[str, float] = {}
    acks: list[float] = []
    for row in rows:
        ft = row.get("fact_type")
        payload = row.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        if ft == LATENCY_SAMPLE_FACT:
            sta = _extract_ms(payload, "submit_to_ack_ms")
            if sta is not None:
                acks.append(sta)
            continue
        if ft not in {OMS_SUBMIT_FACT, OMS_RESULT_FACT}:
            continue
        key = _order_key(payload) or str(row.get("correlation_id") or "")
        if not key:
            continue
        ts = _ts_ms(row.get("ts") or payload.get("register_utc") or payload.get("submit_ts"))
        if ts is None:
            continue
        if ft == OMS_SUBMIT_FACT:
            submits[key] = ts
        elif ft == OMS_RESULT_FACT and key in submits:
            acks.append(ts - submits[key])
        # payload may carry register/submit ack pair directly
        reg = _ts_ms(payload.get("register_utc"))
        ack = _ts_ms(payload.get("submit_ack_utc"))
        if reg is not None and ack is not None and ack >= reg:
            acks.append(ack - reg)
    return acks


def discover_run_fact_files(run_roots: list[Path]) -> list[Path]:
    found: list[Path] = []
    for root in run_roots:
        if not root.exists():
            continue
        for pattern in ("facts.jsonl", "facts/facts.jsonl", "**/facts.jsonl"):
            found.extend(root.glob(pattern))
    return sorted(set(found))


def build_latency_prior(
    *,
    run_roots: list[Path] | None = None,
    default_roots: list[Path] | None = None,
) -> dict[str, Any]:
    """Build latency prior from live fact JSONL files; low confidence if sparse."""
    roots = run_roots or default_roots or [Path("var/runs"), Path("var/live_runs")]
    fact_files = discover_run_fact_files(roots)

    event_to_decision: list[float] = []
    decision_to_submit: list[float] = []
    submit_to_ack: list[float] = []
    total_trigger_to_ack: list[float] = []
    available: set[str] = set()
    missing: set[str] = set()

    for fp in fact_files:
        rows = _read_facts_jsonl(fp)
        partial_sta = _extract_partial_submit_to_ack(rows)
        for val in partial_sta:
            submit_to_ack.append(val)
            available.add("submit_to_ack")

        for row in rows:
            if row.get("fact_type") not in {LATENCY_CHAIN_FACT, *CORRELATION_FACTS, LATENCY_SAMPLE_FACT}:
                continue
            payload = row.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            etd = _event_to_decision_ms(payload)
            if etd is not None:
                event_to_decision.append(etd)
                available.add("event_to_decision")
            dts = _extract_ms(payload, "decision_to_submit_ms")
            if dts is not None:
                decision_to_submit.append(dts)
                available.add("decision_to_submit")
            sta = _extract_ms(payload, "submit_to_ack_ms")
            if sta is not None:
                submit_to_ack.append(sta)
                available.add("submit_to_ack")
            tta = _extract_ms(payload, "trigger_to_submit_ms")
            if tta is not None and sta is not None:
                total_trigger_to_ack.append(tta + sta)
                available.add("total_trigger_to_ack")
            elif _extract_ms(payload, "trigger_to_fill_ms") is not None:
                available.add("trigger_to_fill_ms")

    # Deduplicate missing: only segments never observed
    for seg in ("event_to_decision", "decision_to_submit", "submit_to_ack", "total_trigger_to_ack"):
        if seg not in available:
            missing.add(seg)

    def pack(values: list[float]) -> dict[str, float | None]:
        if not values:
            return {"p50": None, "p90": None, "p95": None}
        return _percentiles(pd.Series(values))

    prior = {
        "p50_event_to_decision_ms": pack(event_to_decision)["p50"],
        "p90_event_to_decision_ms": pack(event_to_decision)["p90"],
        "p95_event_to_decision_ms": pack(event_to_decision)["p95"],
        "p50_decision_to_submit_ms": pack(decision_to_submit)["p50"],
        "p90_decision_to_submit_ms": pack(decision_to_submit)["p90"],
        "p95_decision_to_submit_ms": pack(decision_to_submit)["p95"],
        "p50_submit_to_ack_ms": pack(submit_to_ack)["p50"],
        "p90_submit_to_ack_ms": pack(submit_to_ack)["p90"],
        "p95_submit_to_ack_ms": pack(submit_to_ack)["p95"],
        "p50_total_trigger_to_ack_ms": pack(total_trigger_to_ack)["p50"],
        "p90_total_trigger_to_ack_ms": pack(total_trigger_to_ack)["p90"],
        "p95_total_trigger_to_ack_ms": pack(total_trigger_to_ack)["p95"],
        "source_runs": [str(p) for p in fact_files],
        "available_fields": sorted(available),
        "missing_fields": sorted(missing),
        "confidence": (
            "high"
            if len(total_trigger_to_ack) >= 50
            else ("medium" if len(submit_to_ack) >= 20 else "low")
        ),
        "sample_count_latency_chain": len(submit_to_ack),
        "partial_submit_to_ack_only": bool(submit_to_ack) and "total_trigger_to_ack" in missing,
    }
    return prior


def write_latency_prior(prior: dict[str, Any], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(prior, indent=2), encoding="utf-8")
    return out_path


def load_latency_prior(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def latency_prior_sufficient(prior: dict[str, Any]) -> bool:
    """True when p90 total trigger-to-ack is available."""
    return prior.get("p90_total_trigger_to_ack_ms") is not None or prior.get("p90_submit_to_ack_ms") is not None
