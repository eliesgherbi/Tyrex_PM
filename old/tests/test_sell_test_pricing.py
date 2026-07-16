"""Tests for the sell_test auto-pricing helper.

Pure unit tests that exercise the book-parsing + price-derivation logic with
hand-built book payloads. The async wrapper
:func:`resolve_marketable_price_via_client` is also tested with a stub client
to cover the success / fetch-failure paths without touching the venue.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from tyrex_pm.strategies.sell_test.pricing import (
    ResolvedPrice,
    best_levels_from_book,
    compute_marketable_price,
    resolve_marketable_price_via_client,
)


def _book(bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> dict:
    return {
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": p, "size": s} for p, s in asks],
    }


def test_best_levels_from_book_picks_extremes() -> None:
    book = _book(bids=[("0.40", "10"), ("0.48", "5")], asks=[("0.55", "8"), ("0.62", "3")])
    bid, ask = best_levels_from_book(book)
    assert bid == Decimal("0.48")
    assert ask == Decimal("0.55")


def test_best_levels_from_book_skips_zero_size_levels() -> None:
    book = _book(bids=[("0.49", "0"), ("0.40", "10")], asks=[("0.55", "0"), ("0.62", "3")])
    bid, ask = best_levels_from_book(book)
    assert bid == Decimal("0.40")
    assert ask == Decimal("0.62")


def test_best_levels_from_book_handles_empty_sides() -> None:
    bid, ask = best_levels_from_book({"bids": [], "asks": []})
    assert bid is None
    assert ask is None


def test_best_levels_from_book_tolerates_garbage_input() -> None:
    bid, ask = best_levels_from_book({"bids": "not-a-list", "asks": None})
    assert bid is None
    assert ask is None


def test_compute_marketable_price_buy_picks_best_ask_plus_ticks() -> None:
    rp = compute_marketable_price(
        side="BUY",
        best_bid=Decimal("0.48"),
        best_ask=Decimal("0.55"),
        tick_size=Decimal("0.01"),
        aggression_ticks=1,
        fallback_price=Decimal("0.50"),
    )
    assert rp.source == "auto_book"
    assert rp.price == Decimal("0.56")
    assert rp.error is None


def test_compute_marketable_price_buy_zero_aggression_returns_best_ask() -> None:
    rp = compute_marketable_price(
        side="BUY",
        best_bid=Decimal("0.48"),
        best_ask=Decimal("0.55"),
        tick_size=Decimal("0.01"),
        aggression_ticks=0,
        fallback_price=Decimal("0.50"),
    )
    assert rp.source == "auto_book"
    assert rp.price == Decimal("0.55")


def test_compute_marketable_price_buy_max_price_guardrail_falls_back() -> None:
    rp = compute_marketable_price(
        side="BUY",
        best_bid=Decimal("0.48"),
        best_ask=Decimal("0.95"),
        tick_size=Decimal("0.01"),
        aggression_ticks=1,
        fallback_price=Decimal("0.50"),
        max_price=Decimal("0.80"),
    )
    assert rp.source == "fallback"
    assert rp.price == Decimal("0.50")
    assert "exceeds_max_price" in (rp.error or "")


def test_compute_marketable_price_buy_no_asks_falls_back() -> None:
    rp = compute_marketable_price(
        side="BUY",
        best_bid=Decimal("0.48"),
        best_ask=None,
        tick_size=Decimal("0.01"),
        aggression_ticks=1,
        fallback_price=Decimal("0.50"),
    )
    assert rp.source == "fallback"
    assert rp.error == "no_asks_on_book"
    assert rp.price == Decimal("0.50")


def test_compute_marketable_price_sell_picks_best_bid_minus_ticks() -> None:
    rp = compute_marketable_price(
        side="SELL",
        best_bid=Decimal("0.48"),
        best_ask=Decimal("0.55"),
        tick_size=Decimal("0.01"),
        aggression_ticks=1,
        fallback_price=Decimal("0.49"),
    )
    assert rp.source == "auto_book"
    assert rp.price == Decimal("0.47")


def test_compute_marketable_price_sell_min_price_guardrail_falls_back() -> None:
    rp = compute_marketable_price(
        side="SELL",
        best_bid=Decimal("0.05"),
        best_ask=Decimal("0.06"),
        tick_size=Decimal("0.01"),
        aggression_ticks=2,
        fallback_price=Decimal("0.04"),
        min_price=Decimal("0.04"),
    )
    assert rp.source == "fallback"
    assert "below_min_price" in (rp.error or "")


def test_compute_marketable_price_clamps_to_tradeable_interior() -> None:
    # best_ask near the upper edge: candidate would exceed 1 - tick.
    rp = compute_marketable_price(
        side="BUY",
        best_bid=Decimal("0.97"),
        best_ask=Decimal("0.99"),
        tick_size=Decimal("0.01"),
        aggression_ticks=1,
        fallback_price=Decimal("0.95"),
    )
    assert rp.source == "auto_book"
    # Clamped to 1 - tick = 0.99 (not 1.00 which the venue would reject).
    assert rp.price == Decimal("0.99")


def test_compute_marketable_price_quantizes_floor_to_tick_grid() -> None:
    # tick=0.05 with aggressive aggregator that lands off-grid.
    rp = compute_marketable_price(
        side="BUY",
        best_bid=Decimal("0.40"),
        best_ask=Decimal("0.43"),  # off-grid input on a 0.05 tick market
        tick_size=Decimal("0.05"),
        aggression_ticks=1,
        fallback_price=Decimal("0.40"),
    )
    # candidate = 0.43 + 0.05 = 0.48 -> floor to 0.45 grid step.
    assert rp.source == "auto_book"
    assert rp.price == Decimal("0.45")


def test_compute_marketable_price_unknown_side_falls_back() -> None:
    rp = compute_marketable_price(
        side="HOLD",
        best_bid=Decimal("0.48"),
        best_ask=Decimal("0.55"),
        tick_size=Decimal("0.01"),
        aggression_ticks=1,
        fallback_price=Decimal("0.50"),
    )
    assert rp.source == "fallback"
    assert rp.error and "unknown_side" in rp.error


def test_resolved_price_to_evidence_is_jsonable() -> None:
    rp = ResolvedPrice(
        price=Decimal("0.56"),
        source="auto_book",
        best_ask=Decimal("0.55"),
        best_bid=Decimal("0.48"),
        tick_size=Decimal("0.01"),
        aggression_ticks=1,
        error=None,
    )
    ev = rp.to_evidence()
    assert ev["price"] == "0.56"
    assert ev["best_ask"] == "0.55"
    assert ev["best_bid"] == "0.48"
    assert ev["aggression_ticks"] == 1
    assert ev["error"] is None


# ----------------------- async wrapper tests -----------------------------------


class _StubClient:
    def __init__(self, book: Any | None = None, raise_exc: Exception | None = None) -> None:
        self._book = book
        self._raise = raise_exc
        self.calls: list[str] = []

    def get_order_book(self, token_id: str) -> Any:
        self.calls.append(token_id)
        if self._raise is not None:
            raise self._raise
        return self._book


class _StubMarketInfo:
    def __init__(self, tick_size: Decimal) -> None:
        self.tick_size = tick_size


def test_resolve_via_client_success_uses_book_and_market_info_tick() -> None:
    book = _book(bids=[("0.45", "10")], asks=[("0.55", "8")])
    client = _StubClient(book=book)
    mi = _StubMarketInfo(tick_size=Decimal("0.01"))
    rp = asyncio.run(
        resolve_marketable_price_via_client(
            client=client,
            market_info=mi,
            token_id="tok-x",
            side="BUY",
            aggression_ticks=2,
            fallback_price=Decimal("0.50"),
        )
    )
    assert rp.source == "auto_book"
    assert rp.price == Decimal("0.57")
    assert rp.tick_size == Decimal("0.01")
    assert client.calls == ["tok-x"]


def test_resolve_via_client_falls_back_on_fetch_exception() -> None:
    client = _StubClient(raise_exc=RuntimeError("network down"))
    rp = asyncio.run(
        resolve_marketable_price_via_client(
            client=client,
            market_info=None,
            token_id="tok-x",
            side="BUY",
            aggression_ticks=1,
            fallback_price=Decimal("0.50"),
        )
    )
    assert rp.source == "fallback"
    assert rp.price == Decimal("0.50")
    assert rp.best_ask is None
    assert rp.best_bid is None
    assert rp.error and "book_fetch_failed" in rp.error


def test_resolve_via_client_uses_default_tick_when_market_info_missing() -> None:
    book = _book(bids=[("0.45", "10")], asks=[("0.55", "8")])
    client = _StubClient(book=book)
    rp = asyncio.run(
        resolve_marketable_price_via_client(
            client=client,
            market_info=None,
            token_id="tok-x",
            side="SELL",
            aggression_ticks=1,
            fallback_price=Decimal("0.40"),
        )
    )
    assert rp.source == "auto_book"
    # Default tick = 0.01, so 0.45 - 0.01 = 0.44.
    assert rp.price == Decimal("0.44")


@pytest.mark.parametrize(
    "fb,best_ask,expected_price",
    [
        (None, None, Decimal("0")),
        (Decimal("0.30"), None, Decimal("0.30")),
    ],
)
def test_resolve_no_fallback_returns_zero(
    fb: Decimal | None, best_ask: Decimal | None, expected_price: Decimal
) -> None:
    asks = [] if best_ask is None else [(str(best_ask), "1")]
    book = _book(bids=[("0.40", "1")], asks=asks)
    client = _StubClient(book=book)
    rp = asyncio.run(
        resolve_marketable_price_via_client(
            client=client,
            market_info=None,
            token_id="tok-x",
            side="BUY",
            aggression_ticks=1,
            fallback_price=fb,
        )
    )
    if best_ask is None:
        assert rp.source == "fallback"
        assert rp.price == expected_price
