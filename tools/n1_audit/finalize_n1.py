#!/usr/bin/env python3
"""Finalize N1 window comparisons + latency summary (read-only).

Writes:
  var/reporting/n1/window_comparisons.jsonl
  var/reporting/n1/analysis_summary.json
  Docs/.../n1_latency_sample.jsonl  (sanitized small sample)
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.n1_audit.analyze_capture import (  # noqa: E402
    bps,
    causal_pair,
    first_ge,
    last_le,
    load_jsonl,
    parse_ts,
    ticks_from,
)
from tools.n1_audit.poll_displayed_ptb import extract_open_close, get_html  # noqa: E402
CAPTURE = ROOT / "var/reporting/n1/raw_capture.jsonl"
OUT_WINDOWS = ROOT / "var/reporting/n1/window_comparisons.jsonl"
OUT_SUMMARY = ROOT / "var/reporting/n1/analysis_summary.json"
OUT_SAMPLE = ROOT / "Docs/implementation/z_gap_production_readiness/n1_latency_sample.jsonl"

EPOCHS = [1784582100, 1784582400, 1784582700]
RULES = (
    "first_source_ts_ge_event_start",
    "last_source_ts_le_event_start",
    "nearest_source_ts_to_event_start",
)


def latency_stats(ticks):
    if len(ticks) < 2:
        return {"count": len(ticks)}
    gaps = [
        (ticks[i].source_ts - ticks[i - 1].source_ts).total_seconds() * 1000.0
        for i in range(1, len(ticks))
    ]
    delays = [(t.receive_wall - t.source_ts).total_seconds() * 1000.0 for t in ticks]
    return {
        "count": len(ticks),
        "source_gap_ms_p50": statistics.median(gaps),
        "source_gap_ms_p95": sorted(gaps)[int(0.95 * (len(gaps) - 1))],
        "receive_minus_source_ms_p50": statistics.median(delays),
        "receive_minus_source_ms_p95": sorted(delays)[int(0.95 * (len(delays) - 1))],
        "negative_delay_lt_minus_5ms": sum(1 for d in delays if d < -5),
    }


def main() -> None:
    rows = load_jsonl(CAPTURE)
    cl = ticks_from(rows, "rtds_chainlink", "btc")
    spot = ticks_from(rows, "binance_spot_trade", "btc")
    rtds_bn = ticks_from(rows, "rtds_binance", "btc")

    ooo = sum(1 for r in rows if r.get("late_or_out_of_order"))
    reconnects = [
        r
        for r in rows
        if r.get("source") in ("rtds_control", "binance_control")
        and str(r.get("value", "")).startswith("reconnect")
    ]

    comparisons = []
    for epoch in EPOCHS:
        slug = f"btc-updown-5m-{epoch}"
        boundary = datetime.fromtimestamp(epoch, tz=timezone.utc)
        start_iso = boundary.strftime("%Y-%m-%dT%H:%M:%SZ")
        html = get_html(slug)
        ptb = extract_open_close(html, start_iso)
        displayed = ptb["openPrice"] if ptb and ptb.get("matched_start") else None
        first = first_ge(cl, boundary)
        last = last_le(cl, boundary)
        near = min(cl, key=lambda t: abs((t.source_ts - boundary).total_seconds())) if cl else None
        rule_ticks = {
            "first_source_ts_ge_event_start": first,
            "last_source_ts_le_event_start": last,
            "nearest_source_ts_to_event_start": near,
        }
        for rule_id, tick in rule_ticks.items():
            if tick is None or displayed is None:
                status = "INCOMPLETE"
                rec = {
                    "window_id": slug,
                    "event_start_utc": boundary.isoformat(),
                    "candidate_ptb_rule": rule_id,
                    "candidate_k": None,
                    "displayed_attested_ptb": displayed,
                    "displayed_provenance": "ssr_openPrice_matched_queryKey"
                    if displayed is not None
                    else None,
                    "exact_diff": None,
                    "diff_bps": None,
                    "result": status,
                    "source_ts": None,
                    "receive_wall_utc": None,
                    "receive_monotonic_ns": tick.receive_mono if tick else None,
                    "clock_uncertainty_ms": None,
                    "capture_sequence_note": "see raw_capture.jsonl by source_ts+fingerprint",
                    "boundary_source_lag_ms": None,
                    "boundary_receive_lag_ms": None,
                }
            else:
                diff = tick.value - displayed
                db = bps(tick.value, displayed)
                status = "MATCH" if abs(db) <= 0.05 else "MISMATCH"
                rec = {
                    "window_id": slug,
                    "event_start_utc": boundary.isoformat(),
                    "candidate_ptb_rule": rule_id,
                    "candidate_k": tick.value,
                    "displayed_attested_ptb": displayed,
                    "displayed_provenance": "ssr_dehydrated_react_query_openPrice",
                    "exact_diff": diff,
                    "diff_bps": db,
                    "result": status,
                    "source_ts": tick.source_ts.isoformat(),
                    "receive_wall_utc": tick.receive_wall.isoformat(),
                    "receive_monotonic_ns": tick.receive_mono,
                    "clock_uncertainty_ms": None,
                    "capture_sequence_note": "correlated via source_ts+value in raw_capture.jsonl",
                    "boundary_source_lag_ms": (tick.source_ts - boundary).total_seconds() * 1000.0,
                    "boundary_receive_lag_ms": (tick.receive_wall - boundary).total_seconds()
                    * 1000.0,
                }
            comparisons.append(rec)

    # causal basis samples in windows
    basis = []
    for epoch in EPOCHS:
        for t in cl:
            if not (epoch <= t.source_ts.timestamp() < epoch + 300):
                continue
            p = causal_pair(t, spot)
            if p is None:
                continue
            basis.append(
                {
                    "skew_ms": (t.source_ts - p.source_ts).total_seconds() * 1000.0,
                    "basis_bps": math.log(t.value / p.value) * 10000.0,
                }
            )

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "windows": [f"btc-updown-5m-{e}" for e in EPOCHS],
        "counts": {
            "capture_rows": len(rows),
            "rtds_chainlink": len(cl),
            "binance_spot_trade": len(spot),
            "rtds_binance": len(rtds_bn),
            "ooo_or_late_markers": ooo,
            "reconnect_events": len(reconnects),
        },
        "latency": {
            "rtds_chainlink": latency_stats(cl),
            "binance_spot_trade": latency_stats(spot),
            "rtds_binance": latency_stats(rtds_bn),
        },
        "causal_basis_LATEST_BINANCE_AT_OR_BEFORE_CHAINLINK": {
            "sample_count": len(basis),
            "skew_ms_p50": statistics.median([b["skew_ms"] for b in basis]) if basis else None,
            "skew_ms_p95": sorted([b["skew_ms"] for b in basis])[
                int(0.95 * (len(basis) - 1))
            ]
            if basis
            else None,
            "basis_bps_p50": statistics.median([b["basis_bps"] for b in basis]) if basis else None,
            "basis_bps_p95_abs": sorted([abs(b["basis_bps"]) for b in basis])[
                int(0.95 * (len(basis) - 1))
            ]
            if basis
            else None,
        },
        "boundary_rule_note": (
            "All three sampled windows had a Chainlink tick with source_ts exactly equal "
            "to event_start; first_ge and last_le therefore select the same tick and both "
            "MATCH displayed openPrice at 0 bps. This validates plumbing + provisional "
            "equality to openPrice but does not alone prove which inequality rule Polymarket "
            "uses when no exact-on-boundary tick exists."
        ),
        "comparisons": comparisons,
    }

    OUT_WINDOWS.parent.mkdir(parents=True, exist_ok=True)
    with OUT_WINDOWS.open("w", encoding="utf-8") as fp:
        for rec in comparisons:
            fp.write(json.dumps(rec, separators=(",", ":")) + "\n")
    OUT_SUMMARY.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # sanitized sample: ~30 rows spanning sources near first boundary
    sample = []
    boundary0 = datetime.fromtimestamp(EPOCHS[0], tz=timezone.utc)
    for r in rows:
        src = r.get("source")
        if src not in ("rtds_chainlink", "binance_spot_trade", "rtds_binance"):
            continue
        st = parse_ts(r.get("source_ts"))
        if st is None:
            continue
        if abs((st - boundary0).total_seconds()) > 2.0:
            continue
        sample.append(
            {
                "capture_sequence": r.get("capture_sequence"),
                "source": src,
                "symbol": r.get("symbol"),
                "window_id": r.get("window_id"),
                "source_ts": r.get("source_ts"),
                "receive_wall_utc": r.get("receive_wall_utc"),
                "receive_monotonic_ns": r.get("receive_monotonic_ns"),
                "clock_uncertainty_ms": r.get("clock_uncertainty_ms"),
                "value": r.get("value"),
                "late_or_out_of_order": r.get("late_or_out_of_order"),
                "connection_id": r.get("connection_id"),
                "raw_event_fingerprint": r.get("raw_event_fingerprint"),
            }
        )
        if len(sample) >= 40:
            break
    # also include the three MATCH comparison rows once
    for rec in comparisons:
        if rec["candidate_ptb_rule"] == "first_source_ts_ge_event_start":
            sample.append(
                {
                    "record_type": "window_comparison",
                    **{k: rec[k] for k in rec},
                }
            )
    OUT_SAMPLE.parent.mkdir(parents=True, exist_ok=True)
    with OUT_SAMPLE.open("w", encoding="utf-8") as fp:
        for rec in sample:
            fp.write(json.dumps(rec, separators=(",", ":")) + "\n")

    print(json.dumps({k: summary[k] for k in ("windows", "counts", "latency", "causal_basis_LATEST_BINANCE_AT_OR_BEFORE_CHAINLINK")}, indent=2))
    print("MATCH rows", sum(1 for c in comparisons if c["result"] == "MATCH"), "/", len(comparisons))


if __name__ == "__main__":
    main()
