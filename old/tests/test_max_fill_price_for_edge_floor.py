"""Tests for model-capped fill price helper (A0.6)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tyrex_pm.quant.entry_cap import (
    EntryCapError,
    max_fill_price_for_edge_floor,
    predicted_edge_at_limit,
    quantize_price_down,
)
from tyrex_pm.quant.fees import FeeModel, FEE_MODEL_STATUS_RESOLVED, FEE_MODEL_STATUS_UNKNOWN, phi_taker_fee

TICK = Decimal("0.01")


def _resolved_model(*, r: str = "0.07", e: str = "1") -> FeeModel:
    return FeeModel(
        fee_model_id="polymarket_dynamic_fd_v1",
        fee_model_status=FEE_MODEL_STATUS_RESOLVED,
        fd_r=Decimal(r),
        fd_e=Decimal(e),
        fd_to=True,
    )


def _zero_fee_model() -> FeeModel:
    return _resolved_model(r="0", e="1")


def test_zero_fee_case() -> None:
    p_L = Decimal("0.60")
    floor = Decimal("0.03")
    slip = Decimal("0.01")
    cap = max_fill_price_for_edge_floor(p_L, floor, _zero_fee_model(), slip, TICK)
    edge = predicted_edge_at_limit(p_L=p_L, limit_price=cap, fee_model=_zero_fee_model(), expected_slippage=slip)
    assert edge >= floor
    assert cap == Decimal("0.55")


def test_dynamic_fee_fd_r_007() -> None:
    model = _resolved_model()
    p_L = Decimal("0.60")
    floor = Decimal("0.03")
    slip = Decimal("0.01")
    cap = max_fill_price_for_edge_floor(p_L, floor, model, slip, TICK)
    edge = predicted_edge_at_limit(p_L=p_L, limit_price=cap, fee_model=model, expected_slippage=slip)
    assert edge >= floor
    assert cap < p_L


def test_cap_below_ask_lowers_final_limit() -> None:
    model = _resolved_model()
    p_L = Decimal("0.55")
    floor = Decimal("0.05")
    slip = Decimal("0.01")
    cap = max_fill_price_for_edge_floor(p_L, floor, model, slip, TICK)
    ask = Decimal("0.58")
    final_limit = min(ask, cap)
    assert final_limit == cap
    assert final_limit < ask


def test_quantization_never_raises_above_cap() -> None:
    model = _resolved_model()
    p_L = Decimal("0.62")
    floor = Decimal("0.02")
    slip = Decimal("0.005")
    cap = max_fill_price_for_edge_floor(p_L, floor, model, slip, TICK)
    assert cap == quantize_price_down(cap, TICK)
    bumped = cap + TICK
    with pytest.raises(EntryCapError):
        # artificially high price may violate floor — cap itself must be max feasible tick
        edge_bumped = predicted_edge_at_limit(p_L=p_L, limit_price=bumped, fee_model=model, expected_slippage=slip)
        if edge_bumped < floor:
            raise EntryCapError("bumped violates floor")


def test_returned_price_satisfies_inequality() -> None:
    model = _resolved_model()
    p_L = Decimal("0.70")
    floor = Decimal("0.04")
    slip = Decimal("0.02")
    cap = max_fill_price_for_edge_floor(p_L, floor, model, slip, TICK)
    edge = predicted_edge_at_limit(p_L=p_L, limit_price=cap, fee_model=model, expected_slippage=slip)
    assert edge >= floor


def test_unknown_fee_model_fails_closed() -> None:
    model = FeeModel(
        fee_model_id="polymarket_dynamic_fd_v1",
        fee_model_status=FEE_MODEL_STATUS_UNKNOWN,
        fd_r=None,
        fd_e=None,
        fd_to=None,
    )
    with pytest.raises(EntryCapError, match="unknown"):
        max_fill_price_for_edge_floor(Decimal("0.6"), Decimal("0.03"), model, Decimal("0"), TICK)


def test_no_feasible_price_fails_closed() -> None:
    model = _resolved_model()
    with pytest.raises(EntryCapError, match="no feasible"):
        max_fill_price_for_edge_floor(Decimal("0.05"), Decimal("0.10"), model, Decimal("0.01"), TICK)


def test_invalid_tick_size_fails_closed() -> None:
    model = _resolved_model()
    with pytest.raises(EntryCapError, match="invalid tick"):
        max_fill_price_for_edge_floor(Decimal("0.6"), Decimal("0.03"), model, Decimal("0"), Decimal("0"))


def test_boundary_prices_near_zero_and_one() -> None:
    model = _zero_fee_model()
    cap_lo = max_fill_price_for_edge_floor(Decimal("0.10"), Decimal("0.01"), model, Decimal("0"), TICK)
    assert cap_lo >= Decimal("0")
    assert cap_lo <= Decimal("0.09")
    cap_hi = max_fill_price_for_edge_floor(Decimal("0.99"), Decimal("0.01"), model, Decimal("0"), TICK)
    assert cap_hi <= Decimal("0.98")
    phi = phi_taker_fee(cap_hi, _resolved_model())
    assert phi >= 0
