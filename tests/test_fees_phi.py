"""Tests for dynamic fee curve φ(price) (A0.4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from tyrex_pm.quant.fees import (
    FEE_MODEL_ID_DYNAMIC_FD_V1,
    FEE_MODEL_STATUS_RESOLVED,
    FEE_MODEL_STATUS_UNKNOWN,
    FeeModel,
    curve_parameters_hash,
    flat_fee_from_bps,
    parse_fee_model_from_market_info,
    parse_fee_model_from_raw,
    phi_taker_fee,
    validate_fee_price,
)

LIVE_FD_RAW = {
    "c": "0x507f97b1",
    "fd": {"r": 0.07, "e": 1, "to": True},
    "mbf": 1000,
    "tbf": 1000,
}


def _resolved_model() -> FeeModel:
    return parse_fee_model_from_raw(LIVE_FD_RAW, condition_id="0x507f97b1", market_id="btc_5m_test")


def test_parse_fd_from_raw() -> None:
    model = _resolved_model()
    assert model.fee_model_id == FEE_MODEL_ID_DYNAMIC_FD_V1
    assert model.fee_model_status == FEE_MODEL_STATUS_RESOLVED
    assert model.fd_r == Decimal("0.07")
    assert model.fd_e == Decimal("1")
    assert model.fd_to is True


def test_phi_peak_at_midpoint() -> None:
    model = _resolved_model()
    mid = phi_taker_fee(Decimal("0.50"), model)
    low = phi_taker_fee(Decimal("0.10"), model)
    high = phi_taker_fee(Decimal("0.90"), model)
    assert mid > low
    assert mid > high
    assert low == high


def test_phi_symmetry_at_tails() -> None:
    model = _resolved_model()
    assert phi_taker_fee(Decimal("0.10"), model) == phi_taker_fee(Decimal("0.90"), model)


def test_phi_zero_at_boundaries() -> None:
    model = _resolved_model()
    assert phi_taker_fee(Decimal("0"), model) == Decimal("0")
    assert phi_taker_fee(Decimal("1"), model) == Decimal("0")


def test_phi_live_sample_values() -> None:
    model = _resolved_model()
    assert phi_taker_fee(Decimal("0.50"), model) == Decimal("0.0175")
    assert phi_taker_fee(Decimal("0.10"), model) == Decimal("0.0063")


def test_missing_fd_returns_unknown() -> None:
    model = parse_fee_model_from_raw({"c": "0xabc"}, market_id="m1")
    assert model.fee_model_status == FEE_MODEL_STATUS_UNKNOWN
    assert model.is_resolved is False


def test_invalid_price_rejected() -> None:
    model = _resolved_model()
    with pytest.raises(ValueError, match="\\[0, 1\\]"):
        validate_fee_price(Decimal("-0.01"))
    with pytest.raises(ValueError, match="\\[0, 1\\]"):
        phi_taker_fee(Decimal("1.01"), model)


def test_decimal_precision_stable() -> None:
    model = _resolved_model()
    fee = phi_taker_fee(Decimal("0.45"), model)
    # 0.07 * 0.45 * 0.55 = 0.017325
    assert fee == Decimal("0.017325")


def test_flat_fee_rate_bps_not_used_for_dynamic_phi() -> None:
    model = _resolved_model()
    price = Decimal("0.45")
    dynamic = phi_taker_fee(price, model)
    flat = flat_fee_from_bps(price, 1000)
    assert flat == Decimal("0.045")
    assert dynamic != flat
    assert dynamic < flat


def test_parse_ignores_fee_rate_bps_even_when_present() -> None:
    @dataclass
    class _MI:
        condition_id: str = "0x507f97b1"
        fee_rate_bps: int = 1000
        raw: dict = field(default_factory=lambda: dict(LIVE_FD_RAW))

    model = parse_fee_model_from_market_info(_MI(), market_id="btc_5m_test")
    assert model.is_resolved is True
    assert model.fd_r == Decimal("0.07")
    # Dynamic phi must not equal flat bps fee at 0.45
    assert phi_taker_fee(Decimal("0.45"), model) != flat_fee_from_bps(Decimal("0.45"), 1000)


def test_no_double_counting_fd_and_fee_rate() -> None:
    """Parser uses only ``fd``; flat ``fee_rate_bps`` does not alter φ."""
    raw = dict(LIVE_FD_RAW)
    model_a = parse_fee_model_from_raw(raw, fee_rate_bps=1000)
    model_b = parse_fee_model_from_raw(raw, fee_rate_bps=5000)
    price = Decimal("0.50")
    assert phi_taker_fee(price, model_a) == phi_taker_fee(price, model_b)


def test_curve_parameters_hash_stable() -> None:
    model = _resolved_model()
    h1 = curve_parameters_hash(fd_r=model.fd_r, fd_e=model.fd_e, fd_to=model.fd_to)
    h2 = curve_parameters_hash(fd_r=Decimal("0.07"), fd_e=Decimal("1"), fd_to=True)
    assert h1 is not None
    assert h1 == h2


def test_phi_unknown_model_raises() -> None:
    unknown = parse_fee_model_from_raw({})
    with pytest.raises(ValueError, match="not resolved"):
        phi_taker_fee(Decimal("0.5"), unknown)
