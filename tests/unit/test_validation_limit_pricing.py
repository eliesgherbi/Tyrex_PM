"""validation_limit_pricing — Scenario A marketable limits (unit)."""

from __future__ import annotations

import pytest

from tyrex_pm.execution.c3_book_top import BookTop
from tyrex_pm.strategy.validation_limit_pricing import (
    aggressive_validation_buy_limit,
    aggressive_validation_sell_limit,
)


def test_buy_with_ask_anchor_is_above_reference_and_ask() -> None:
    book = BookTop(
        best_bid=0.40,
        best_ask=0.42,
        best_bid_size=10.0,
        best_ask_size=10.0,
        source="cache",
    )
    q = aggressive_validation_buy_limit(
        reference_price=0.41,
        tick_size=0.01,
        book=book,
        aggression_ticks=1,
        max_slippage_fraction=0.2,
    )
    assert q.limit_price >= 0.42
    assert q.limit_price == pytest.approx(0.43, abs=1e-9)
    assert q.anchor_description == "best_ask"


def test_sell_with_bid_anchor_is_at_or_below_bid() -> None:
    book = BookTop(
        best_bid=0.55,
        best_ask=0.56,
        best_bid_size=1.0,
        best_ask_size=1.0,
        source="cache",
    )
    q = aggressive_validation_sell_limit(
        reference_price=0.555,
        tick_size=0.01,
        book=book,
        aggression_ticks=1,
        max_slippage_fraction=0.2,
    )
    assert q.limit_price <= 0.55
    assert q.anchor_description == "best_bid"


def test_buy_slippage_clamps_high() -> None:
    book = BookTop(
        best_bid=0.90,
        best_ask=0.95,
        best_bid_size=1.0,
        best_ask_size=1.0,
        source="cache",
    )
    q = aggressive_validation_buy_limit(
        reference_price=0.50,
        tick_size=0.01,
        book=book,
        aggression_ticks=10,
        max_slippage_fraction=0.02,
    )
    assert "slippage_cap" in q.clamp_note
    assert q.limit_price == pytest.approx(0.50 * 1.02, abs=0.02)


def test_no_book_uses_reference_plus_ticks_buy() -> None:
    book = BookTop(None, None, None, None, "none")
    q = aggressive_validation_buy_limit(
        reference_price=0.40,
        tick_size=0.01,
        book=book,
        aggression_ticks=2,
        max_slippage_fraction=0.5,
    )
    assert q.anchor_description == "reference_only"
    assert q.limit_price == pytest.approx(0.42, abs=1e-9)
