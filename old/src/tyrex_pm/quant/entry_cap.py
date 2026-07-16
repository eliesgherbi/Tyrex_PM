"""Model-capped fill price for Z-Gap entry (A0.6).

Constraint (post-fill edge floor):
    p_L - limit_price - phi(limit_price) - expected_slippage >= theta_fill_floor
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

from tyrex_pm.quant.fees import FeeModel, phi_taker_fee, validate_fee_price

BINARY_PRICE_MIN = Decimal("0")
BINARY_PRICE_MAX = Decimal("1")
_BINARY_SEARCH_STEPS = 80


class EntryCapError(ValueError):
    """Raised when model cap cannot be computed safely (fail closed)."""


def quantize_price_down(price: Decimal, tick_size: Decimal) -> Decimal:
    """Floor ``price`` to the nearest tick multiple (never round up)."""
    if tick_size <= 0:
        raise EntryCapError("invalid tick size")
    ticks = (price / tick_size).to_integral_value(rounding=ROUND_DOWN)
    return ticks * tick_size


def predicted_edge_at_limit(
    *,
    p_L: Decimal,
    limit_price: Decimal,
    fee_model: FeeModel,
    expected_slippage: Decimal,
) -> Decimal:
    """Post-fill model edge at ``limit_price`` after fee and slippage."""
    validate_fee_price(limit_price)
    phi = phi_taker_fee(limit_price, fee_model)
    return p_L - limit_price - phi - expected_slippage


def max_fill_price_for_edge_floor(
    p_L: Decimal,
    theta_fill_floor: Decimal,
    fee_model: FeeModel,
    expected_slippage: Decimal,
    tick_size: Decimal,
) -> Decimal:
    """Highest fill price that still satisfies the post-fill edge floor.

    Quantized down to ``tick_size``. The returned price satisfies the inequality
    after quantization.
    """
    if not fee_model.is_resolved:
        raise EntryCapError("fee model unknown")
    if tick_size <= 0:
        raise EntryCapError("invalid tick size")
    if tick_size > BINARY_PRICE_MAX:
        raise EntryCapError("invalid tick size")

    try:
        validate_fee_price(p_L)
    except ValueError as exc:
        raise EntryCapError(str(exc)) from exc

    if expected_slippage < 0:
        raise EntryCapError("expected_slippage must be non-negative")

    edge_at_zero = predicted_edge_at_limit(
        p_L=p_L,
        limit_price=BINARY_PRICE_MIN,
        fee_model=fee_model,
        expected_slippage=expected_slippage,
    )
    if edge_at_zero < theta_fill_floor:
        raise EntryCapError("no feasible price satisfies edge floor")

    lo = BINARY_PRICE_MIN
    hi = BINARY_PRICE_MAX
    for _ in range(_BINARY_SEARCH_STEPS):
        mid = (lo + hi) / Decimal("2")
        edge_mid = predicted_edge_at_limit(
            p_L=p_L,
            limit_price=mid,
            fee_model=fee_model,
            expected_slippage=expected_slippage,
        )
        if edge_mid >= theta_fill_floor:
            lo = mid
        else:
            hi = mid

    candidate = quantize_price_down(lo, tick_size)
    while candidate >= BINARY_PRICE_MIN:
        edge_q = predicted_edge_at_limit(
            p_L=p_L,
            limit_price=candidate,
            fee_model=fee_model,
            expected_slippage=expected_slippage,
        )
        if edge_q >= theta_fill_floor:
            return candidate
        candidate -= tick_size

    raise EntryCapError("no feasible price after tick quantization")
