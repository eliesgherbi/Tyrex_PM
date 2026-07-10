"""Tests for binary fair-value model (A0.3)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.quant.binary_fair_value import (
    FairValueInput,
    compute_fair_value,
    normal_cdf,
)
from tyrex_pm.quant.volatility import SIGMA_UNITS_PER_SQRT_SECOND, VolatilitySnapshot
from tyrex_pm.runtime.z_gap_model_facts import build_model_state_snapshot_payload

GOLDEN_PATH = Path(__file__).resolve().parent / "fixtures" / "z_gap" / "fair_value_golden.json"
UTC = timezone.utc
TS = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


def _load_golden() -> list[dict]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _load_golden(), ids=lambda c: c["name"])
def test_fair_value_golden_vectors(case: dict) -> None:
    inp = FairValueInput(
        S=Decimal(case["S"]) if case.get("S") is not None else None,
        K=Decimal(case["K"]) if case.get("K") is not None else None,
        sigma=case.get("sigma"),
        tau_s=case.get("tau_s"),
        tau_floor_s=case.get("tau_floor_s", 1.0),
        snapshot_ts=TS,
    )
    out = compute_fair_value(inp)
    assert out.model_status == case["model_status"]
    if case["model_status"] == "ready":
        assert out.z == pytest.approx(case["expected_z"], abs=1e-4)
        assert out.p_up == pytest.approx(case["expected_p_up"], abs=1e-3)
        assert out.p_down == pytest.approx(case["expected_p_down"], abs=1e-3)
        assert out.sigma_units == SIGMA_UNITS_PER_SQRT_SECOND
    else:
        assert out.reject_reason == case.get("reject_reason")


def test_normal_cdf_at_zero() -> None:
    assert normal_cdf(0.0) == pytest.approx(0.5)


def test_shorter_tau_increases_abs_z() -> None:
    base = FairValueInput(S=Decimal("110"), K=Decimal("100"), sigma=0.01, tau_s=100, snapshot_ts=TS)
    short = FairValueInput(S=Decimal("110"), K=Decimal("100"), sigma=0.01, tau_s=25, snapshot_ts=TS)
    z_long = compute_fair_value(base).z
    z_short = compute_fair_value(short).z
    assert z_long is not None and z_short is not None
    assert abs(z_short) > abs(z_long)


def test_higher_sigma_decreases_abs_z() -> None:
    low_sig = FairValueInput(S=Decimal("110"), K=Decimal("100"), sigma=0.01, tau_s=100, snapshot_ts=TS)
    high_sig = FairValueInput(S=Decimal("110"), K=Decimal("100"), sigma=0.02, tau_s=100, snapshot_ts=TS)
    z_low = compute_fair_value(low_sig).z
    z_high = compute_fair_value(high_sig).z
    assert z_low is not None and z_high is not None
    assert abs(z_high) < abs(z_low)


def test_missing_sigma_not_ready() -> None:
    out = compute_fair_value(FairValueInput(S=Decimal("100"), K=Decimal("100"), sigma=None, tau_s=60, snapshot_ts=TS))
    assert out.model_status == "not_ready"
    assert out.reject_reason == "missing_sigma"


def test_vol_snapshot_not_ready_blocks_model() -> None:
    vol = VolatilitySnapshot(
        sigma=0.01,
        sigma_units=SIGMA_UNITS_PER_SQRT_SECOND,
        ready=False,
        sample_count=3,
        effective_samples_s=3.0,
        last_update_ts=TS,
        jump_guard_tripped=False,
        reject_reason="min_samples_not_met",
    )
    out = compute_fair_value(
        FairValueInput(S=Decimal("100"), K=Decimal("100"), sigma=0.01, tau_s=60, snapshot_ts=TS),
        vol=vol,
    )
    assert out.model_status == "not_ready"
    assert out.reject_reason == "min_samples_not_met"


def test_model_state_snapshot_payload_contract() -> None:
    fair = compute_fair_value(
        FairValueInput(S=Decimal("110"), K=Decimal("100"), sigma=0.01, tau_s=100, snapshot_ts=TS)
    )
    vol = VolatilitySnapshot(
        sigma=0.01,
        sigma_units=SIGMA_UNITS_PER_SQRT_SECOND,
        ready=True,
        sample_count=25,
        effective_samples_s=25.0,
        last_update_ts=TS,
        jump_guard_tripped=False,
        reject_reason=None,
    )
    payload = build_model_state_snapshot_payload(fair, vol=vol)
    assert payload["sigma_units"] == SIGMA_UNITS_PER_SQRT_SECOND
    assert payload["sigma_ready"] is True
    assert payload["sample_count"] == 25
    assert payload["jump_guard_tripped"] is False
    assert payload["p_up"] is not None
