#!/usr/bin/env python3
"""Run time-triggered Z-Gap observe-only live windows (A0.5 validation)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from tyrex_pm.ingestion.btc_5m_window_scheduler import (
    BTC_5M_WINDOW_S,
    btc_5m_canonical_event_url,
    current_btc_5m_window_start,
)

DEFAULT_SKIP_REPORT_PATH = Path("var/reporting/z_gap/window_skip_report.jsonl")


def _utc_now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _wait_until(ts: float) -> None:
    delay = max(0.0, ts - _utc_now())
    if delay > 0:
        print(f"Waiting {delay:.1f}s until {datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()}")
        time.sleep(delay)


def _append_skip_report(
    *,
    path: Path,
    run_name: str,
    event_start_ts: float,
    reason: str,
    now_ts: float,
    min_prestart_seconds: float,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "fact": "z_gap_window_skipped_late_start",
        "run_name": run_name,
        "event_start_ts": event_start_ts,
        "now_ts": now_ts,
        "late_by_s": now_ts - (event_start_ts - min_prestart_seconds),
        "min_prestart_seconds": min_prestart_seconds,
        "reason": reason,
        "recorded_at_utc": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Batch Z-Gap observe-only windows (time-triggered)")
    parser.add_argument("--windows", type=int, default=3, help="Number of full windows to run")
    parser.add_argument(
        "--prestart-seconds",
        type=float,
        default=60.0,
        help="Launch run this many seconds before window open",
    )
    parser.add_argument(
        "--min-prestart-seconds",
        type=float,
        default=20.0,
        help="Skip window if launch would occur later than event_start - this value",
    )
    parser.add_argument(
        "--strategy",
        default="config/strategies/z_gap.yaml",
    )
    parser.add_argument(
        "--scenario",
        default="live_z_gap_observe",
    )
    parser.add_argument(
        "--name-prefix",
        default="z_gap_observe",
    )
    parser.add_argument(
        "--skip-report",
        default=str(DEFAULT_SKIP_REPORT_PATH),
        help="JSONL path for z_gap_window_skipped_late_start rows",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned windows without executing",
    )
    args = parser.parse_args(argv)

    now = _utc_now()
    start = current_btc_5m_window_start(now) + BTC_5M_WINDOW_S
    if now >= start + 30:
        start += BTC_5M_WINDOW_S

    planned: list[tuple[str, str, float, float]] = []
    for i in range(args.windows):
        ws = start + i * BTC_5M_WINDOW_S
        we = ws + BTC_5M_WINDOW_S
        url = btc_5m_canonical_event_url(ws)
        run_name = f"{args.name_prefix}_{int(ws)}"
        planned.append((run_name, url, ws, we))

    print("Planned observe-only windows (time-triggered):")
    for run_name, url, ws, we in planned:
        print(
            f"  {run_name}: {url} "
            f"({datetime.fromtimestamp(ws, tz=timezone.utc).isoformat()} -> "
            f"{datetime.fromtimestamp(we, tz=timezone.utc).isoformat()})"
        )

    if args.dry_run:
        return 0

    skip_path = Path(args.skip_report)
    processes: list[tuple[str, subprocess.Popen[bytes], float]] = []

    for idx, (run_name, url, ws, _we) in enumerate(planned):
        wake = ws - args.prestart_seconds
        _wait_until(wake)
        now_ts = _utc_now()
        latest_allowed = ws - args.min_prestart_seconds
        if now_ts > latest_allowed:
            reason = (
                f"now={now_ts:.3f} > event_start-min_prestart="
                f"{latest_allowed:.3f} (late by {now_ts - latest_allowed:.1f}s)"
            )
            print(f"\n=== SKIP Window {idx + 1}/{args.windows}: {run_name} — {reason} ===")
            _append_skip_report(
                path=skip_path,
                run_name=run_name,
                event_start_ts=ws,
                reason=reason,
                now_ts=now_ts,
                min_prestart_seconds=args.min_prestart_seconds,
            )
            continue

        cmd = [
            sys.executable,
            "-m",
            "tyrex_pm.runtime.app",
            "run",
            "--strategy",
            args.strategy,
            "--scenario",
            args.scenario,
            "--event-url",
            url,
            "--run-name",
            run_name,
        ]
        print(f"\n=== Launch Window {idx + 1}/{args.windows}: {run_name} ===")
        print(" ".join(cmd))
        proc = subprocess.Popen(cmd)
        processes.append((run_name, proc, ws))

    print(f"\nWaiting for {len(processes)} launched window(s)...")
    exit_codes: dict[str, int] = {}
    for run_name, proc, _ws in processes:
        rc = proc.wait()
        exit_codes[run_name] = rc
        print(f"Window {run_name} exit_code={rc}")
        if rc != 0:
            print(f"WARNING: window {run_name} returned non-zero exit code {rc}")

    return 0 if all(rc == 0 for rc in exit_codes.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
