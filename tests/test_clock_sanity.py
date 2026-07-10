"""Tests for clock sanity preflight (A0.1)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tyrex_pm.runtime.time_authority import TimeAuthority
from tyrex_pm.runtime.z_gap_clock_sanity import (
    CLOCK_DRIFT_ENFORCE_THRESHOLD_MS,
    evaluate_clock_sanity,
    write_clock_sanity_report,
)
from tyrex_pm.runtime.z_gap_preflight import load_z_gap_preflight_gates


def _synced_ta(*, uncertainty_ms: float = 20.0) -> TimeAuthority:
    return TimeAuthority(
        sync_status="synced",
        offset_ms=10.0,
        uncertainty_ms=uncertainty_ms,
        median_offset_ms=10.0,
        samples_requested=7,
        samples_kept=3,
        max_rtt_ms=40.0,
        source="sntp_time_cloudflare_com",
        sntp_offset_ms=10.0,
        binance_offset_ms=12.0,
        offset_disagreement_ms=2.0,
        _epoch_at_sync=1_000_000.0,
        _mono_at_sync=100.0,
    )


def test_time_authority_synced_passes_enforce_gate() -> None:
    local = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
    reference = local + timedelta(milliseconds=120)
    report = evaluate_clock_sanity(
        local_dt=local,
        reference_dt=reference,
        reference_source="test",
        time_authority=_synced_ta(),
    )
    assert report.pass_ is True
    assert report.enforce_gate_pass is True
    assert report.enforce_blocked is False


def test_time_authority_high_uncertainty_blocks_enforce() -> None:
    local = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
    reference = local + timedelta(milliseconds=50)
    report = evaluate_clock_sanity(
        local_dt=local,
        reference_dt=reference,
        reference_source="test",
        time_authority=_synced_ta(uncertainty_ms=300.0),
    )
    assert report.pass_ is False
    assert report.enforce_blocked is True
    assert report.fail_reason is not None
    assert "uncertainty_ms" in report.fail_reason


def test_os_drift_informational_when_time_authority_passes() -> None:
    local = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
    reference = local + timedelta(milliseconds=800)
    report = evaluate_clock_sanity(
        local_dt=local,
        reference_dt=reference,
        reference_source="test",
        time_authority=_synced_ta(),
    )
    assert report.pass_ is True
    assert abs(report.os_drift_ms or 0) > 500
    assert report.observe_warning is not None


def test_output_artifact_contains_clock_drift_ms(tmp_path: Path) -> None:
    local = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
    reference = local + timedelta(milliseconds=50)
    report = evaluate_clock_sanity(
        local_dt=local,
        reference_dt=reference,
        reference_source="test",
        time_authority=_synced_ta(),
    )
    out = tmp_path / "clock_sanity.json"
    write_clock_sanity_report(out, report)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert "clock_drift_ms" in loaded
    assert loaded["pass"] is True
    assert loaded["enforce_gate_pass"] is True


def test_preflight_gate_blocks_enforce_on_clock_fail(tmp_path: Path) -> None:
    base = tmp_path / "z_gap"
    base.mkdir(parents=True)
    (base / "fee_curve_spike.json").write_text(
        json.dumps({"outcome": "full_curve_available", "fd": {"r": 0.07, "e": 1}}),
        encoding="utf-8",
    )
    (base / "binance_connectivity.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
    (base / "ptb_attestation.json").write_text(
        json.dumps(
            {
                "attestation_pass": True,
                "ptb_error_bps": "0.1",
                "golden_fixture_only": False,
                "enforce_unlock_allowed": True,
            }
        ),
        encoding="utf-8",
    )
    (base / "clock_sanity.json").write_text(
        json.dumps(
            {
                "sync_status": "failed",
                "enforce_gate_pass": False,
                "clock_drift_ms": 900,
                "fail_reason": "time_authority sync failed",
            }
        ),
        encoding="utf-8",
    )
    (base / "calibration_lite_review.json").write_text(
        json.dumps({"reviewed": True, "operator_signoff": True}),
        encoding="utf-8",
    )
    (base / "operator_enforce_approval.json").write_text(
        json.dumps({"approved": True}),
        encoding="utf-8",
    )
    gates = load_z_gap_preflight_gates(base)
    assert gates.clock_sanity_passed is False
    assert not gates.enforce_allowed
    assert any("clock_sanity_passed" in b for b in gates.blockers)
