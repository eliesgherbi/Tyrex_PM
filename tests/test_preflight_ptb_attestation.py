"""Tests for PTB attestation preflight (A0.1)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.runtime.z_gap_ptb_attestation import (
    PTB_TOLERANCE_BPS,
    derive_k_from_recording,
    evaluate_ptb_attestation,
    ptb_error_bps,
    write_ptb_attestation_report,
)

GOLDEN_RECORDING = Path("tests/fixtures/recordings/golden_day/btc_5m_20260703_1200")


def test_ptb_error_bps_within_tolerance_passes() -> None:
    err = ptb_error_bps(Decimal("109812.50"), Decimal("109812.50"))
    assert err == Decimal("0")
    report = evaluate_ptb_attestation(
        K_derived=Decimal("109812.50"),
        K_reference=Decimal("109812.50"),
        reference_source="manual",
        market_id="btc_5m_20260703_1200",
        event_start_ts=1780000000.0,
        data_source="live_recording",
        golden_fixture_only=False,
        checked_at_utc="2026-07-09T00:00:00+00:00",
    )
    assert report.attestation_pass is True
    assert report.ptb_error_bps == "0"
    assert report.enforce_unlock_allowed is True


def test_ptb_mismatch_above_half_bps_fails() -> None:
    # 1 USD on ~109812 => ~0.09 bps; use larger delta for clear fail
    report = evaluate_ptb_attestation(
        K_derived=Decimal("109812.50"),
        K_reference=Decimal("109824.50"),
        reference_source="manual",
        market_id="m1",
        event_start_ts=1.0,
        data_source="live_recording",
        golden_fixture_only=False,
        checked_at_utc="2026-07-09T00:00:00+00:00",
    )
    assert report.attestation_pass is False
    assert report.ptb_error_bps is not None
    assert Decimal(report.ptb_error_bps) > PTB_TOLERANCE_BPS


def test_golden_fixture_alone_does_not_unlock_enforce() -> None:
    report = evaluate_ptb_attestation(
        K_derived=Decimal("109812.50"),
        K_reference=Decimal("109812.50"),
        reference_source="golden_fixture_test_only",
        market_id="btc_5m_20260703_1200",
        event_start_ts=1780000000.0,
        data_source="golden_day_recording",
        golden_fixture_only=True,
        checked_at_utc="2026-07-09T00:00:00+00:00",
    )
    assert report.attestation_pass is True
    assert report.golden_fixture_only is True
    assert report.enforce_unlock_allowed is False


def test_missing_reference_fails_closed() -> None:
    report = evaluate_ptb_attestation(
        K_derived=Decimal("109812.50"),
        K_reference=None,
        reference_source="unavailable",
        market_id="m1",
        event_start_ts=1.0,
        data_source="live_recording",
        golden_fixture_only=False,
        checked_at_utc="2026-07-09T00:00:00+00:00",
    )
    assert report.attestation_pass is False
    assert "missing K_reference" in (report.attestation_fail_reason or "")


def test_derive_k_from_golden_day_recording() -> None:
    k, start_ts, data_source, golden = derive_k_from_recording(GOLDEN_RECORDING)
    assert k == Decimal("109812.50")
    assert start_ts == 1780000000.0
    assert golden is True
    assert "golden_day" in data_source


def test_output_artifact_contains_ptb_error_bps(tmp_path: Path) -> None:
    report = evaluate_ptb_attestation(
        K_derived=Decimal("109812.50"),
        K_reference=Decimal("109812.51"),
        reference_source="manual",
        market_id="m1",
        event_start_ts=1.0,
        data_source="live_recording",
        golden_fixture_only=False,
        checked_at_utc="2026-07-09T00:00:00+00:00",
    )
    out = tmp_path / "ptb_attestation.json"
    write_ptb_attestation_report(out, report)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert "ptb_error_bps" in loaded
    assert loaded["ptb_error_bps"] is not None
