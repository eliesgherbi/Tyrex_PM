#!/usr/bin/env python3
"""A0.1 — PTB attestation preflight for Z-Gap Phase A.

Validates derived price-to-beat K against a fresh reference before entry_mode: enforce.

Usage:
    python scripts/preflight_ptb_attestation.py \\
        --recording-dir path/to/fresh_recording \\
        --reference-k 109812.50 \\
        --reference-source manual_operator

    python scripts/preflight_ptb_attestation.py \\
        --recording-dir tests/fixtures/recordings/golden_day/btc_5m_20260703_1200 \\
        --reference-k 109812.50 \\
        --reference-source golden_fixture_test_only

Golden_day fixtures may pass attestation for tests but do NOT unlock enforce
(enforce_unlock_allowed=false when golden_fixture_only=true).

Manual/reference input is required when live Gamma/UI PTB is unavailable.
Do not silently pass without a fresh reference.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from tyrex_pm.runtime.z_gap_ptb_attestation import (
    derive_k_from_recording,
    evaluate_ptb_attestation,
    write_ptb_attestation_report,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_ptb_attestation(
    *,
    recording_dir: Path,
    reference_k: Decimal | None,
    reference_source: str,
    market_id: str | None,
    output: Path,
) -> int:
    k_derived, event_start_ts, data_source, golden_only = derive_k_from_recording(
        recording_dir,
        market_id=market_id,
    )
    mid = market_id or "unknown"
    if k_derived is not None and mid == "unknown":
        payload_mid = None
        from tyrex_pm.runtime.z_gap_ptb_attestation import _find_ptb_in_recording

        payload = _find_ptb_in_recording(recording_dir, market_id=None)
        if payload:
            mid = str(payload.get("market_id") or "unknown")

    report = evaluate_ptb_attestation(
        K_derived=k_derived,
        K_reference=reference_k,
        reference_source=reference_source,
        market_id=mid,
        event_start_ts=event_start_ts,
        data_source=data_source,
        golden_fixture_only=golden_only,
        checked_at_utc=_utc_now(),
    )
    write_ptb_attestation_report(output, report)
    print(json.dumps(report.to_dict(), indent=2))
    print(f"\nWrote {output}")
    if report.attestation_pass:
        print("RESULT: PASS")
        if report.golden_fixture_only:
            print("NOTE: golden_fixture_only=true — enforce remains blocked")
        return 0
    print(f"RESULT: FAIL ({report.attestation_fail_reason})")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Z-Gap A0.1 PTB attestation preflight")
    parser.add_argument(
        "--recording-dir",
        required=True,
        help="Directory containing events*.jsonl with price_to_beat_observed events",
    )
    parser.add_argument(
        "--reference-k",
        required=True,
        help="Reference PTB K (manual/Gamma/UI). Required — no silent pass without reference.",
    )
    parser.add_argument(
        "--reference-source",
        default="manual_operator",
        help="Label for reference provenance (e.g. manual_operator, gamma_ui, chainlink_ui)",
    )
    parser.add_argument("--market-id", default=None, help="Optional market_id filter")
    parser.add_argument(
        "--output",
        default="var/reporting/z_gap/ptb_attestation.json",
        help="JSON artifact path",
    )
    args = parser.parse_args(argv)

    recording_dir = Path(args.recording_dir)
    if not recording_dir.is_dir():
        print(f"recording-dir not found: {recording_dir}", file=sys.stderr)
        return 2

    try:
        reference_k = Decimal(str(args.reference_k))
    except Exception as exc:
        print(f"invalid --reference-k: {exc}", file=sys.stderr)
        return 2

    return run_ptb_attestation(
        recording_dir=recording_dir,
        reference_k=reference_k,
        reference_source=str(args.reference_source),
        market_id=args.market_id,
        output=Path(args.output),
    )


if __name__ == "__main__":
    raise SystemExit(main())
