#!/usr/bin/env python3
"""A0.1 — Clock sanity preflight for Z-Gap Phase A.

Runs TimeAuthority SNTP sampling and gates enforce on sync_status + uncertainty_ms.
Raw OS drift vs Binance reference is informational only.

Usage:
    python scripts/preflight_clock_sanity.py
    python scripts/preflight_clock_sanity.py --output var/reporting/z_gap/clock_sanity.json

Reference sources (informational OS drift only):
  1. Binance REST /api/v3/time (serverTime)
  2. --reference-ts manual override (ISO or unix seconds)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from tyrex_pm.runtime.z_gap_clock_sanity import (
    CLOCK_DRIFT_ENFORCE_THRESHOLD_MS,
    evaluate_clock_sanity,
    write_clock_sanity_report,
)
from tyrex_pm.runtime.time_authority import sample_offset


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_reference_ts(raw: str) -> datetime:
    text = raw.strip()
    if text.isdigit():
        return datetime.fromtimestamp(float(text), tz=timezone.utc)
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fetch_binance_reference_time(
    *,
    timeout_s: float = 10.0,
) -> tuple[datetime, str]:
    url = "https://api.binance.com/api/v3/time"
    with httpx.Client(timeout=timeout_s) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
    server_ms = data.get("serverTime")
    if server_ms is None:
        raise ValueError("Binance /api/v3/time missing serverTime")
    return datetime.fromtimestamp(float(server_ms) / 1000.0, tz=timezone.utc), "binance_rest_serverTime"


def resolve_reference_time(
    *,
    reference_ts: str | None,
    use_binance: bool,
    timeout_s: float,
) -> tuple[datetime, str]:
    if reference_ts:
        return _parse_reference_ts(reference_ts), "manual_reference_ts"
    if use_binance:
        return fetch_binance_reference_time(timeout_s=timeout_s)
    raise ValueError("no reference time available (provide --reference-ts or enable Binance lookup)")


def run_clock_sanity(
    *,
    reference_ts: str | None,
    use_binance: bool,
    threshold_ms: float,
    output: Path,
    timeout_s: float,
) -> int:
    local_dt = _utc_now()
    try:
        reference_dt, reference_source = resolve_reference_time(
            reference_ts=reference_ts,
            use_binance=use_binance,
            timeout_s=timeout_s,
        )
    except Exception as exc:
        payload: dict[str, Any] = {
            "local_time_utc": local_dt.isoformat(),
            "reference_time_utc": None,
            "clock_drift_ms": None,
            "reference_source": "unavailable",
            "pass": False,
            "fail_reason": f"reference clock unavailable: {type(exc).__name__}: {exc}",
            "enforce_blocked": True,
            "observe_warning": "observe_only may proceed only after manual clock verification",
            "checked_at_utc": local_dt.isoformat(),
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(json.dumps(payload, indent=2))
        print(f"\nWrote {output}")
        print("RESULT: FAIL")
        return 1

    report = evaluate_clock_sanity(
        local_dt=local_dt,
        reference_dt=reference_dt,
        reference_source=reference_source,
        threshold_ms=threshold_ms,
        time_authority=sample_offset(require_feeds_not_started=False, timeout_s=timeout_s),
    )
    write_clock_sanity_report(output, report)
    print(json.dumps(report.to_dict(), indent=2))
    print(f"\nWrote {output}")
    if report.enforce_gate_pass:
        print("RESULT: PASS (TimeAuthority enforce gate)")
        if report.observe_warning:
            print(f"WARN: {report.observe_warning}")
        return 0
    print(f"RESULT: FAIL ({report.fail_reason})")
    if report.observe_warning:
        print(f"WARN: {report.observe_warning}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Z-Gap A0.1 clock sanity preflight")
    parser.add_argument(
        "--reference-ts",
        default=None,
        help="Manual trusted UTC reference (ISO-8601 or unix seconds)",
    )
    parser.add_argument(
        "--no-binance",
        action="store_true",
        help="Skip Binance REST serverTime lookup",
    )
    parser.add_argument(
        "--threshold-ms",
        type=float,
        default=CLOCK_DRIFT_ENFORCE_THRESHOLD_MS,
        help="Informational OS drift warning threshold (ms); enforce uses TimeAuthority",
    )
    parser.add_argument("--timeout-s", type=float, default=10.0)
    parser.add_argument(
        "--output",
        default="var/reporting/z_gap/clock_sanity.json",
        help="JSON artifact path",
    )
    args = parser.parse_args(argv)

    return run_clock_sanity(
        reference_ts=args.reference_ts,
        use_binance=not args.no_binance,
        threshold_ms=args.threshold_ms,
        output=Path(args.output),
        timeout_s=args.timeout_s,
    )


if __name__ == "__main__":
    raise SystemExit(main())
