#!/usr/bin/env python3
"""
Summarize ``rtds_primary`` soak from the Nautilus log (``run_guru.py`` file sink).

Validates operator goals:
  - **No duplicate live submits:** more than one ``live_order_submit`` per ``correlation_id``.
  - **Stable RTDS:** counts of reconnect / stall / fallback activation vs clear (flapping hint).
  - **Gap-fill:** ``guru_gap_fill`` / ``guru_gap_fill_begin`` / ``guru_gap_fill_error`` summary.
  - **Latency:** ``detection_to_emit_ms`` on ``guru_signal_emitted`` (rtds vs poll vs gap_fill);
    ``signal_to_submit_ms`` on ``live_order_intent``.
  - **Host RTDS load:** not in logs; script prints a reminder to check CPU / task manager / msg rates.

Usage::

  python scripts/guru_primary_report.py logs/live/run_nautilus.log

Interpretation:
  - ``detection_to_emit_ms`` can be **negative** if ``ts_event_ms`` is ahead of wall clock (exchange/event time).
  - ``live_order_intent`` lines may appear **out of order** vs ``guru_signal_emitted`` in the same file; use counts, not line adjacency.
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

RE_EMITTED = re.compile(
    r"event=guru_signal_emitted\s+[^\n]*\bsource=(?P<src>poll|rtds|gap_fill)\b[^\n]*"
    r"correlation_id=(?P<cid>.+?)\s+side=\S+.*?ts_event_ms=(?P<tev>\d+)"
    r"(?:\s+detection_to_emit_ms=(?P<dte>-?\d+))?",
)
RE_LIVE_SUBMIT = re.compile(
    r"event=live_order_submit\s+[^\n]*correlation_id=(?P<cid>.+?)\s+client_order_id=",
)
RE_LIVE_INTENT = re.compile(
    r"event=live_order_intent\s+[^\n]*correlation_id=(?P<cid>.+?)\s+"
    r"signal_kind=\S+\s+side=\S+\s+qty=\S+\s+.*?signal_to_submit_ms=(?P<sts>\d+)",
)
RE_GAP_FILL = re.compile(
    r"event=guru_gap_fill\s+component=(?P<comp>\S+)\s+rows=(?P<rows>\d+)\s+"
    r"published=(?P<pub>\d+)\s+ts_fill_ms=(?P<ts>\d+)",
)
RE_FALLBACK_ACT = re.compile(r"event=guru_ingest_fallback_activation\b")
RE_FALLBACK_CLR = re.compile(r"event=guru_ingest_fallback_cleared\b")


def in_path_lines(path: Path):
    with path.open(encoding="utf-8", errors="replace") as f:
        yield from f


def _fmt_stats(values: list[int]) -> str:
    if not values:
        return "n=0"
    s = sorted(values)
    med = statistics.median(s)
    return f"n={len(s)} min={s[0]} p50={med:.0f} max={s[-1]}"


def parse_primary(path: Path) -> dict:
    emit_lines_by_cid_src: dict[tuple[str, str], int] = defaultdict(int)
    dte_by_src: dict[str, list[int]] = defaultdict(list)
    cids_by_src: dict[str, set[str]] = defaultdict(set)

    submit_by_cid: dict[str, int] = defaultdict(int)
    intent_by_cid: dict[str, int] = defaultdict(int)
    sts_values: list[int] = []

    gap_fills: list[tuple[str, int, int, str]] = []
    gap_fill_errors = 0
    gap_fill_begins = 0

    health: dict[str, int] = defaultdict(int)
    fallback_seq: list[str] = []

    for line in in_path_lines(path):
        m = RE_EMITTED.search(line)
        if m:
            src = m.group("src")
            cid = m.group("cid")
            emit_lines_by_cid_src[(cid, src)] += 1
            cids_by_src[src].add(cid)
            dte = m.group("dte")
            if dte is not None:
                dte_by_src[src].append(int(dte))
            continue

        m = RE_LIVE_SUBMIT.search(line)
        if m:
            submit_by_cid[m.group("cid")] += 1
            continue

        m = RE_LIVE_INTENT.search(line)
        if m:
            cid = m.group("cid")
            intent_by_cid[cid] += 1
            sts_values.append(int(m.group("sts")))
            continue

        m = RE_GAP_FILL.search(line)
        if m:
            gap_fills.append(
                (m.group("comp"), int(m.group("rows")), int(m.group("pub")), m.group("ts")),
            )
            continue

        if "event=guru_gap_fill_error" in line:
            gap_fill_errors += 1
            continue
        if "event=guru_gap_fill_begin" in line:
            gap_fill_begins += 1
            continue

        if RE_FALLBACK_ACT.search(line):
            fallback_seq.append("activation")
            continue
        if RE_FALLBACK_CLR.search(line):
            fallback_seq.append("cleared")
            continue

        if "event=guru_rtds_reconnect" in line or "guru_rtds_reconnect scheduled_backoff" in line:
            health["guru_rtds_reconnect_any"] += 1
            continue
        if "event=guru_rtds_stall idle_s=" in line:
            health["guru_rtds_stall_ws_idle"] += 1
            continue
        if "event=guru_rtds_stall" in line:
            health["guru_rtds_stall_actor"] += 1
            continue
        if "event=guru_rtds_disconnect" in line:
            health["guru_rtds_disconnect"] += 1
            continue
        if "event=guru_rtds_connect_attempt" in line:
            health["guru_rtds_connect_attempt"] += 1
            continue
        if "event=guru_rtds_subscribed" in line:
            health["guru_rtds_subscribed"] += 1
            continue
        if "event=guru_rtds_ws_close" in line:
            health["guru_rtds_ws_close"] += 1
            continue
        if "event=guru_rtds_ws_error" in line:
            health["guru_rtds_ws_error"] += 1
            continue
        if "event=guru_rtds_ping_error" in line:
            health["guru_rtds_ping_error"] += 1
            continue

    dup_emits = [(cid, src, n) for (cid, src), n in emit_lines_by_cid_src.items() if n > 1]
    dup_submit = [(cid, n) for cid, n in submit_by_cid.items() if n > 1]
    dup_intent = [(cid, n) for cid, n in intent_by_cid.items() if n > 1]

    all_emit_cids = {cid for cid, _ in emit_lines_by_cid_src}
    cid_to_srcs: dict[str, set[str]] = defaultdict(set)
    for (cid, src), n in emit_lines_by_cid_src.items():
        if n >= 1:
            cid_to_srcs[cid].add(src)
    multi_src_cids = [cid for cid, srcs in cid_to_srcs.items() if len(srcs) > 1]

    max_act_streak = streak = 0
    for ev in fallback_seq:
        if ev == "activation":
            streak += 1
            max_act_streak = max(max_act_streak, streak)
        else:
            streak = 0

    return {
        "cids_by_src": {k: len(v) for k, v in sorted(cids_by_src.items())},
        "emit_dupes": dup_emits,
        "dte_by_src": dte_by_src,
        "dup_submit": dup_submit,
        "dup_intent": dup_intent,
        "submit_total": sum(submit_by_cid.values()),
        "submit_unique": len(submit_by_cid),
        "intent_total": sum(intent_by_cid.values()),
        "sts_values": sts_values,
        "gap_fills": gap_fills,
        "gap_fill_errors": gap_fill_errors,
        "gap_fill_begins": gap_fill_begins,
        "health": dict(health),
        "fallback_act": sum(1 for x in fallback_seq if x == "activation"),
        "fallback_clr": sum(1 for x in fallback_seq if x == "cleared"),
        "fallback_seq_len": len(fallback_seq),
        "fallback_max_act_streak": max_act_streak,
        "multi_src_cids": multi_src_cids,
        "all_emit_cids_n": len(all_emit_cids),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "log_file",
        type=Path,
        nargs="?",
        default=Path("logs/live/run_nautilus.log"),
        help="Nautilus log path",
    )
    parser.add_argument(
        "--max-print",
        type=int,
        default=20,
        help="Max duplicate ids to print",
    )
    args = parser.parse_args()
    p = args.log_file
    if not p.is_file():
        print(f"ERROR: not a file: {p}", file=sys.stderr)
        return 1

    r = parse_primary(p)
    mp = max(0, args.max_print)

    print(f"file={p.resolve()}")
    print("\n## ingest (guru_signal_emitted unique correlation_ids by source)")
    for src, n in sorted(r["cids_by_src"].items()):
        print(f"  {src}: {n}")
    print(f"  total_distinct_ids: {r['all_emit_cids_n']}")

    if r["multi_src_cids"]:
        print(
            f"\n  WARNING: same correlation_id from multiple sources (unexpected): "
            f"{len(r['multi_src_cids'])}",
        )
        for _, cid in enumerate(sorted(r["multi_src_cids"])[:mp]):
            print(f"    {cid}")
    else:
        print("  multi_source_ids: 0  (each id from at most one emit source)")

    if r["emit_dupes"]:
        print(f"\n  WARNING: duplicate guru_signal_emitted lines (same id+source): {len(r['emit_dupes'])}")
        for i, (cid, src, n) in enumerate(sorted(r["emit_dupes"])[:mp]):
            print(f"    {src} x{n} {cid}")
    else:
        print("  duplicate_emit_lines: 0")

    print("\n## detection_to_emit_ms (guru_signal_emitted; negative = event clock ahead of wall)")
    for src in ("rtds", "poll", "gap_fill"):
        xs = r["dte_by_src"].get(src, [])
        print(f"  {src}: {_fmt_stats(xs)}")

    print("\n## live execution")
    print(f"  live_order_submit: total_lines={r['submit_total']} distinct_correlation_id={r['submit_unique']}")
    if r["dup_submit"]:
        print(f"  WARNING duplicate_submit_same_correlation_id: {len(r['dup_submit'])}")
        for i, (cid, n) in enumerate(sorted(r["dup_submit"])[:mp]):
            print(f"    x{n} {cid}")
    else:
        print("  duplicate_live_order_submit: 0")

    print(
        f"  live_order_intent: total_lines={r['intent_total']} "
        f"signal_to_submit_ms {_fmt_stats(r['sts_values'])}",
    )
    if r["dup_intent"]:
        print(f"  WARNING duplicate_intent_same_correlation_id: {len(r['dup_intent'])}")
        for i, (cid, n) in enumerate(sorted(r["dup_intent"])[:mp]):
            print(f"    x{n} {cid}")
    else:
        print("  duplicate_live_order_intent: 0")

    print("\n## gap_fill")
    print(f"  guru_gap_fill_begin: {r['gap_fill_begins']}")
    print(f"  guru_gap_fill_error: {r['gap_fill_errors']}")
    if r["gap_fills"]:
        pub = sum(t[2] for t in r["gap_fills"])
        rows = sum(t[1] for t in r["gap_fills"])
        print(f"  guru_gap_fill rows_sum={rows} published_sum={pub} runs={len(r['gap_fills'])}")
        for comp, rowc, pubc, ts in r["gap_fills"][-5:]:
            print(f"    component={comp} rows={rowc} published={pubc} ts_fill_ms={ts}")
    else:
        print("  guru_gap_fill: (no summary lines; reconnect may not have triggered gap-fill)")

    print("\n## fallback / RTDS stability (flapping = many activations or long activation streak)")
    print(f"  fallback_activation: {r['fallback_act']}")
    print(f"  fallback_cleared: {r['fallback_clr']}")
    print(f"  max_consecutive_activation_without_prior_clear_in_seq: {r['fallback_max_act_streak']}")
    if r["health"]:
        print("  rtds_related_counts:")
        for k in sorted(r["health"].keys()):
            print(f"    {k}: {r['health'][k]}")
    else:
        print("  rtds_related_counts: (none)")

    print("\n## operational (not from log)")
    print("  Unfiltered RTDS load: use Task Manager / perf mon; optional spike script msg/s.")
    print("  Compare detection_to_emit rtds vs poll when fallback poll samples exist.")

    bad = bool(r["dup_submit"] or r["emit_dupes"] or r["multi_src_cids"] or r["dup_intent"])
    return 0 if not bad else 2


if __name__ == "__main__":
    raise SystemExit(main())
