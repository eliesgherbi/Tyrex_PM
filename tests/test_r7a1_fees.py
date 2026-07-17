"""R7A.1 fee descriptor parsing and $5 collateral cap including fees."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tyrex_pm.execution.polymarket.fees_fd import (
    FEE_PARAMETERS_UNKNOWN,
    FeeError,
    max_buy_amount_under_collateral_cap,
    parse_fd,
    taker_fee_usdc_for_buy_amount,
    taker_fee_usdc_for_shares,
)
from tyrex_pm.execution.polymarket.order_sizing import (
    BLOCKED_MINIMUM_ORDER_EXCEEDS_AUTHORIZED_CAP,
    SizingError,
    size_buy_under_cap,
)


def test_parse_fd_crypto() -> None:
    fd = parse_fd({"fd": {"r": 0.07, "e": 1, "to": True}}, condition_id="0xabc")
    assert fd.fee_rate == Decimal("0.07")
    assert fd.exponent == Decimal("1")
    assert fd.taker_only is True


def test_unknown_fee_blocks() -> None:
    with pytest.raises(FeeError, match=FEE_PARAMETERS_UNKNOWN):
        parse_fd({})


def test_buy_fee_matches_docs_formula() -> None:
    # fee = C * r * p * (1-p); C = A/p → fee = A * r * (1-p) for e=1
    fee = parse_fd({"fd": {"r": 0.07, "e": 1, "to": True}})
    amount = Decimal("5.00")
    price = Decimal("0.50")
    got = taker_fee_usdc_for_buy_amount(
        buy_amount_usdc=amount, price=price, fee=fee
    )
    # 5 * 0.07 * 0.5 = 0.175
    assert got == Decimal("0.17500") or got == Decimal("0.175")
    shares = Decimal("100")
    # docs table crypto @0.50 / 100 shares = 1.75
    assert taker_fee_usdc_for_shares(shares=shares, price=price, fee=fee) == Decimal(
        "1.75000"
    ) or taker_fee_usdc_for_shares(shares=shares, price=price, fee=fee) == Decimal(
        "1.75"
    )


def test_cap_includes_fee() -> None:
    fee = parse_fd({"fd": {"r": 0.07, "e": 1, "to": True}})
    amount = max_buy_amount_under_collateral_cap(
        collateral_cap=Decimal("5.00"),
        price=Decimal("0.53"),
        fee=fee,
    )
    fee_u = taker_fee_usdc_for_buy_amount(
        buy_amount_usdc=amount, price=Decimal("0.53"), fee=fee
    )
    assert amount + fee_u <= Decimal("5.00")
    assert amount < Decimal("5.00")


def test_sizing_requires_fee_and_respects_cap() -> None:
    with pytest.raises(SizingError, match=FEE_PARAMETERS_UNKNOWN):
        size_buy_under_cap(
            best_ask=Decimal("0.52"),
            ask_size=Decimal("1000"),
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("5"),
            fee=None,
        )
    fee = parse_fd({"fd": {"r": 0.07, "e": 1, "to": True}})
    s = size_buy_under_cap(
        best_ask=Decimal("0.52"),
        ask_size=Decimal("1000"),
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("5"),
        fee=fee,
    )
    assert s.amount + s.estimated_buy_fee <= Decimal("5.00")
    assert s.max_collateral <= Decimal("5.00")


def test_min_order_with_fee_can_block() -> None:
    fee = parse_fd({"fd": {"r": 0.07, "e": 1, "to": True}})
    with pytest.raises(SizingError, match=BLOCKED_MINIMUM_ORDER_EXCEEDS_AUTHORIZED_CAP):
        size_buy_under_cap(
            best_ask=Decimal("0.99"),
            ask_size=Decimal("100"),
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("10"),
            fee=fee,
        )
