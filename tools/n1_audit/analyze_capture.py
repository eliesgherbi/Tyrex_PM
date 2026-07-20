#!/usr/bin/env python3
"""Analyze N1 capture JSONL: boundary rules, latency, causal basis."""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WINDOW_S = 300


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return rows


@dataclass
class Tick:
    source_ts: datetime
    receive_wall: datetime
    value: float
    receive_mono: int | None = None


def ticks_from(rows: list[dict[str, Any]], source: str, symbol_substr: str) -> list[Tick]:
    out: list[Tick] = []
    for r in rows:
        if r.get("source") != source:
            continue
        if symbol_substr.lower() not in str(r.get("symbol", "")).lower():
            continue
        st = parse_ts(r.get("source_ts"))
        rw = parse_ts(r.get("receive_wall_utc"))
        try:
            val = float(r.get("value"))
        except (TypeError, ValueError):
            continue
        if st is None or rw is None:
            continue
        out.append(Tick(st, rw, val, r.get("receive_monotonic_ns")))
    out.sort(key=lambda t: t.source_ts)
    return out


def first_ge(ticks: list[Tick], boundary: datetime) -> Tick | None:
    for t in ticks:
        if t.source_ts >= boundary:
            return t
    return None


def last_le(ticks: list[Tick], boundary: datetime) -> Tick | None:
    cand = None
    for t in ticks:
        if t.source_ts <= boundary:
            cand = t
        else:
            break
    return cand


def causal_pair(cl: Tick, bn_ticks: list[Tick]) -> Tick | None:
    cand = None
    for t in bn_ticks:
        if t.source_ts <= cl.source_ts:
            cand = t
        else:
            break
    return cand


def bps(a: float, b: float) -> float:
    return (a - b) / b * 10000.0 if b else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", type=Path, default=Path("var/reporting/n1/raw_capture.jsonl"))
    ap.add_argument("--ptb", type=Path, default=Path("var/reporting/n1/displayed_ptb.jsonl"))
    ap.add_argument("--out", type=Path, default=Path("var/reporting/n1/analysis_summary.json"))
    ap.add_argument(
        "--windows",
        nargs="*",
        default=[],
        help="window epochs (unix seconds). Default: infer complete windows from PTB+capture",
    )
    args = ap.parse_args()

    rows = load_jsonl(args.capture)
    ptb_rows = load_jsonl(args.ptb)
    cl = ticks_from(rows, "rtds_chainlink", "btc")
    rtds_bn = ticks_from(rows, "rtds_binance", "btc")
    spot = ticks_from(rows, "binance_spot_trade", "btc")

    # displayed PTB by window (latest observation)
    displayed: dict[str, dict[str, Any]] = {}
    for r in ptb_rows:
        wid = r["window_id"]
        displayed[wid] = {
            "openPrice": float(r["value"]),
            "closePrice": (r.get("extra") or {}).get("closePrice"),
            "receive_wall_utc": r.get("receive_wall_utc"),
            "matched_start": (r.get("extra") or {}).get("matched_start"),
        }

    if args.windows:
        epochs = [int(x) for x in args.windows]
    else:
        epochs = sorted({int(w.split("-")[-1]) for w in displayed})
        # keep only windows where we have chainlink coverage spanning boundary
        epochs = [
            e
            for e in epochs
            if any(t.source_ts.timestamp() >= e for t in cl)
            and any(t.source_ts.timestamp() <= e + 5 for t in cl)
        ]

    per_window: list[dict[str, Any]] = []
    for epoch in epochs:
        wid = f"btc-updown-5m-{epoch}"
        boundary = datetime.fromtimestamp(epoch, tz=timezone.utc)
        first = first_ge(cl, boundary)
        last = last_le(cl, boundary)
        disp = displayed.get(wid, {}).get("openPrice")
        rules = {}
        for rule_id, tick in (
            ("first_source_ts_ge_event_start", first),
            ("last_source_ts_le_event_start", last),
        ):
            if tick is None or disp is None:
                rules[rule_id] = {"status": "INCOMPLETE"}
                continue
            diff = tick.value - disp
            rules[rule_id] = {
                "candidate_k": tick.value,
                "displayed_k": disp,
                "diff": diff,
                "diff_bps": bps(tick.value, disp),
                "source_ts": tick.source_ts.isoformat(),
                "receive_wall_utc": tick.receive_wall.isoformat(),
                "boundary_lag_ms": (tick.source_ts - boundary).total_seconds() * 1000.0,
                "receive_lag_from_boundary_ms": (tick.receive_wall - boundary).total_seconds()
                * 1000.0,
                "status": "MATCH" if abs(bps(tick.value, disp)) <= 0.05 else "MISMATCH",
            }
        # nearest absolute by source_ts
        near = None
        if cl:
            near = min(cl, key=lambda t: abs((t.source_ts - boundary).total_seconds()))
        if near is not None and disp is not None:
            rules["nearest_source_ts_to_event_start"] = {
                "candidate_k": near.value,
                "displayed_k": disp,
                "diff": near.value - disp,
                "diff_bps": bps(near.value, disp),
                "source_ts": near.source_ts.isoformat(),
                "offset_ms": (near.source_ts - boundary).total_seconds() * 1000.0,
                "status": "MATCH" if abs(bps(near.value, disp)) <= 0.05 else "MISMATCH",
            }

        basis_samples = []
        for t in cl:
            if not (epoch <= t.source_ts.timestamp() < epoch + WINDOW_S):
                continue
            pair_src = spot if spot else rtds_bn
            p = causal_pair(t, pair_src)
            if p is None:
                continue
            skew_ms = (t.source_ts - p.source_ts).total_seconds() * 1000.0
            import math

            basis = math.log(t.value / p.value)
            basis_samples.append({"skew_ms": skew_ms, "basis_ln": basis, "basis_bps_approx": basis * 10000})

        per_window.append(
            {
                "window_id": wid,
                "event_start": boundary.isoformat(),
                "displayed": displayed.get(wid),
                "rules": rules,
                "basis_sample_count": len(basis_samples),
                "basis_skew_ms_p50": statistics.median([b["skew_ms"] for b in basis_samples])
                if basis_samples
                else None,
                "basis_bps_p50": statistics.median([b["basis_bps_approx"] for b in basis_samples])
                if basis_samples
                else None,
            }
        )

    def latency_stats(ticks: list[Tick]) -> dict[str, Any]:
        if len(ticks) < 2:
            return {"count": len(ticks)}
        gaps = [
            (ticks[i].source_ts - ticks[i - 1].source_ts).total_seconds() * 1000.0
            for i in range(1, len(ticks))
        ]
        recv_delays = [
            (t.receive_wall - t.source_ts).total_seconds() * 1000.0 for t in ticks
        ]
        return {
            "count": len(ticks),
            "source_gap_ms_p50": statistics.median(gaps),
            "source_gap_ms_p95": sorted(gaps)[int(0.95 * (len(gaps) - 1))],
            "receive_minus_source_ms_p50": statistics.median(recv_delays),
            "receive_minus_source_ms_p95": sorted(recv_delays)[
                int(0.95 * (len(recv_delays) - 1))
            ],
            "ooo_or_negative_delay_count": sum(1 for d in recv_delays if d < -5),
        }

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "capture_rows": len(rows),
            "ptb_rows": len(ptb_rows),
            "rtds_chainlink_btc": len(cl),
            "rtds_binance_btc": len(rtds_bn),
            "binance_spot_trade": len(spot),
        },
        "latency": {
            "rtds_chainlink": latency_stats(cl),
            "rtds_binance": latency_stats(rtds_bn),
            "binance_spot_trade": latency_stats(spot),
        },
        "per_window": per_window,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2)[:8000])


if __name__ == "__main__":
    main()
