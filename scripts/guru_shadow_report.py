#!/usr/bin/env python3
"""
Summarize rtds_shadow run: poll vs stream correlation_id coverage (no log noise).

Reads Nautilus file log (same sink as ``run_guru.py``) and prints:
  - counts for ``guru_signal_emitted source=poll`` vs ``guru_stream_would_emit``
  - correlation_ids only on poll, only on stream, on both
  - rough timing: for ids on both, how often stream ``ts_recv_ms`` < poll ``ts_emit_ms``
  - RTDS health: reconnect / stall / fallback counts

Usage (repo root):

  python scripts/guru_shadow_report.py logs/live/run_nautilus.log
  python scripts/guru_shadow_report.py logs/live/rtds_shadow_nautilus.log

Interpretation (rtds_shadow):
  ``would_publish_new=False`` on a stream line means poll already dedup-published;
  that is normal, not "RTDS missed". Use **presence** of ``correlation_id`` on both
  line types plus **timestamps** to see who saw the trade first.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

RE_POLL_LINE = re.compile(r"event=guru_signal_emitted\s+[^\n]*\bsource=poll\b")
RE_CID = re.compile(r"correlation_id=(?P<cid>.+?)\s+side=\S+")
RE_TS_EMIT = re.compile(r"\bts_emit_ms=(?P<v>\d+)")
RE_STREAM = re.compile(
    r"event=guru_stream_would_emit\s+[^\n]*correlation_id=(?P<cid>.+?)\s+side=\S+\s+token_id=\S+\s+"
    r"ts_event_ms=\d+\s+ts_recv_ms=(?P<ts_recv>\d+)\s+would_publish_new=(?P<new>\S+)",
)

RE_HEALTH = re.compile(
    r"event=(guru_rtds_reconnect|guru_rtds_stall|guru_ingest_fallback_activation|"
    r"guru_rtds_disconnect|guru_gap_fill_begin)\b",
)


def _parse_log(path: Path) -> tuple[dict[str, int], dict[str, int], dict[str, dict]]:
    poll_first_emit: dict[str, int] = {}
    stream_first_recv: dict[str, int] = {}

    for line in in_path_lines(path):
        if RE_POLL_LINE.search(line):
            mc = RE_CID.search(line)
            me = RE_TS_EMIT.search(line)
            if mc and me:
                cid = mc.group("cid")
                emit = int(me.group("v"))
                if cid not in poll_first_emit or emit < poll_first_emit[cid]:
                    poll_first_emit[cid] = emit
            continue
        m = RE_STREAM.search(line)
        if m:
            cid = m.group("cid")
            tr = int(m.group("ts_recv"))
            if cid not in stream_first_recv or tr < stream_first_recv[cid]:
                stream_first_recv[cid] = tr

    health: dict[str, int] = defaultdict(int)
    for line in in_path_lines(path):
        hm = RE_HEALTH.search(line)
        if hm:
            health[hm.group(1)] += 1

    return poll_first_emit, stream_first_recv, dict(health)


def in_path_lines(path: Path):
    with path.open(encoding="utf-8", errors="replace") as f:
        yield from f


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "log_file",
        type=Path,
        nargs="?",
        default=Path("logs/live/run_nautilus.log"),
        help="Nautilus log path (default: logs/live/run_nautilus.log)",
    )
    parser.add_argument(
        "--max-print",
        type=int,
        default=15,
        help="Max IDs to print per only-poll / only-stream section (default: 15)",
    )
    args = parser.parse_args()
    p = args.log_file
    if not p.is_file():
        print(f"ERROR: not a file: {p}", file=sys.stderr)
        return 1

    poll_emit, stream_recv, health = _parse_log(p)
    set_poll = set(poll_emit)
    set_stream = set(stream_recv)
    both = set_poll & set_stream
    only_poll = set_poll - set_stream
    only_stream = set_stream - set_poll

    print(f"file={p.resolve()}")
    print(f"poll_emits:     {len(set_poll)}  (event=guru_signal_emitted source=poll)")
    print(f"stream_would:   {len(set_stream)}  (event=guru_stream_would_emit)")
    print(f"both:           {len(both)}")
    print(f"only_poll:      {len(only_poll)}")
    print(f"only_stream:    {len(only_stream)}")

    stream_ahead = 0
    poll_ahead = 0
    tie = 0
    for cid in both:
        se = stream_recv[cid]
        pe = poll_emit[cid]
        if se < pe:
            stream_ahead += 1
        elif se > pe:
            poll_ahead += 1
        else:
            tie += 1
    if both:
        print(
            f"timing (stream ts_recv vs poll ts_emit, first line each): "
            f"stream_first={stream_ahead} poll_first={poll_ahead} same_ms={tie}",
        )

    if health:
        print("rtds_guru_events:")
        for k in sorted(health.keys()):
            print(f"  {k}: {health[k]}")
    else:
        print("rtds_guru_events: (none matched - check log path or run length)")

    mp = max(0, args.max_print)
    if only_poll and mp:
        print(f"\n--- sample only_poll ({min(mp, len(only_poll))} of {len(only_poll)}) ---")
        for i, cid in enumerate(sorted(only_poll)):
            if i >= mp:
                break
            print(f"  {cid}")
    if only_stream and mp:
        print(f"\n--- sample only_stream ({min(mp, len(only_stream))} of {len(only_stream)}) ---")
        for i, cid in enumerate(sorted(only_stream)):
            if i >= mp:
                break
            print(f"  {cid}")

    print("\nNotes:")
    print("  only_poll: REST lag, RTDS drop/filter, or narrow time window (poll backfill vs live stream).")
    print("  only_stream: rare; parser/wallet/filter or poll not yet observing that id.")
    print("  would_publish_new on stream lines: False after poll dedup is expected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
