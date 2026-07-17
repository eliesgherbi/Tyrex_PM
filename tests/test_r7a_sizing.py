"""R7A/R7A.1 order sizing under $5 cap including fees."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tyrex_pm.execution.polymarket.fees_fd import parse_fd
from tyrex_pm.execution.polymarket.order_sizing import (
    BLOCKED_MINIMUM_ORDER_EXCEEDS_AUTHORIZED_CAP,
    SizingError,
    size_buy_under_cap,
)


def _fee():
    return parse_fd({"fd": {"r": 0.07, "e": 1, "to": True}})


def test_sizing_never_exceeds_5_with_fee() -> None:
    s = size_buy_under_cap(
        best_ask=Decimal("0.52"),
        ask_size=Decimal("1000"),
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("5"),
        max_buy_notional=Decimal("5.00"),
        fee=_fee(),
    )
    assert s.amount + s.estimated_buy_fee <= Decimal("5.00")
    assert s.quantity >= Decimal("5")
    assert s.order_type == "FAK"
    assert "GUARANTEED" not in s.reason


def test_rounding_cannot_exceed_cap() -> None:
    s = size_buy_under_cap(
        best_ask=Decimal("0.33"),
        ask_size=Decimal("100"),
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("1"),
        max_buy_notional=Decimal("5.00"),
        fee=_fee(),
    )
    assert s.max_collateral <= Decimal("5.00")


def test_min_order_exceeds_cap_blocked() -> None:
    with pytest.raises(SizingError, match=BLOCKED_MINIMUM_ORDER_EXCEEDS_AUTHORIZED_CAP):
        size_buy_under_cap(
            best_ask=Decimal("0.99"),
            ask_size=Decimal("100"),
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("10"),
            max_buy_notional=Decimal("5.00"),
            fee=_fee(),
        )
