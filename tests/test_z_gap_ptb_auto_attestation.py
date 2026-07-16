"""Tests for PTB auto-attestation and commissioning policy."""

from __future__ import annotations

import json
import time

from tyrex_pm.runtime.z_gap_ptb_commissioning import (
    COMMISSIONING_POLICY_NAME,
    validate_commissioning_certificate,
)


def test_commissioning_requires_min_windows() -> None:
    now = time.time()
    result = validate_commissioning_certificate(
        {"policy_name": COMMISSIONING_POLICY_NAME, "ptb_config_hash": "h1", "windows": []},
        ptb_config_hash="h1",
        now_ts=now,
    )
    assert not result.valid


def test_commissioning_rejects_stale_certificate() -> None:
    now = time.time()
    cert = {
        "policy_name": COMMISSIONING_POLICY_NAME,
        "ptb_config_hash": "h1",
        "issued_at_ts": now - 86400 * 2,
        "windows": [{"ptb_error_bps": 0.1}, {"ptb_error_bps": 0.1}, {"ptb_error_bps": 0.1}],
    }
    result = validate_commissioning_certificate(cert, ptb_config_hash="h1", now_ts=now, max_age_hours=24.0)
    assert not result.valid


def test_commissioning_accepts_valid_certificate() -> None:
    now = time.time()
    cert = {
        "policy_name": COMMISSIONING_POLICY_NAME,
        "policy_id": COMMISSIONING_POLICY_NAME,
        "status": "valid",
        "ptb_config_hash": "h1",
        "issued_at_ts": now - 3600,
        "windows": [
            {"ptb_error_bps": 0.1, "usable": True},
            {"ptb_error_bps": 0.2, "usable": True},
            {"ptb_error_bps": 0.0, "usable": True},
        ],
    }
    result = validate_commissioning_certificate(cert, ptb_config_hash="h1", now_ts=now)
    assert result.valid
    assert result.windows_validated == 3
