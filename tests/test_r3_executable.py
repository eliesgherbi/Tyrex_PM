"""Executable book view boundary tests."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tyrex_pm.core.ids import InstrumentId
from tyrex_pm.core.snapshots import BookLevel, BookSnapshot
from tyrex_pm.market_data.executable import book_quote, executable_vwap


TS = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def test_empty_book_quote() -> None:
    q = book_quote(None)
    assert q.best_bid is None and q.mid is None and q.spread is None


def test_one_sided_book() -> None:
    book = BookSnapshot(
        instrument_id=InstrumentId("x"),
        ts_event=TS,
        bids=(BookLevel(price=Decimal("0.4"), quantity=Decimal("10")),),
        asks=(),
    )
    q = book_quote(book)
    assert q.best_bid == Decimal("0.4")
    assert q.best_ask is None
    assert q.mid is None


def test_price_boundaries_zero_and_one() -> None:
    book = BookSnapshot(
        instrument_id=InstrumentId("x"),
        ts_event=TS,
        bids=(BookLevel(price=Decimal("0"), quantity=Decimal("1")),),
        asks=(BookLevel(price=Decimal("1"), quantity=Decimal("1")),),
    )
    q = book_quote(book)
    assert q.best_bid == Decimal("0")
    assert q.best_ask == Decimal("1")
    assert q.mid == Decimal("0.5")
    assert q.spread == Decimal("1")


def test_bid_ask_ordering() -> None:
    book = BookSnapshot.from_levels(
        instrument_id=InstrumentId("x"),
        ts_event=TS,
        bids=[("0.40", "1"), ("0.45", "2"), ("0.42", "3")],
        asks=[("0.60", "1"), ("0.55", "2"), ("0.58", "3")],
    )
    assert book.bids[0].price == Decimal("0.45")
    assert book.asks[0].price == Decimal("0.55")


def test_vwap_insufficient_depth() -> None:
    levels = (
        BookLevel(price=Decimal("0.5"), quantity=Decimal("2")),
        BookLevel(price=Decimal("0.51"), quantity=Decimal("2")),
    )
    result = executable_vwap(levels, Decimal("10"), side="BUY")
    assert result.sufficient is False
    assert result.filled_qty == Decimal("4")
    assert result.vwap == (Decimal("0.5") * 2 + Decimal("0.51") * 2) / Decimal("4")


def test_vwap_exact() -> None:
    levels = (BookLevel(price=Decimal("0.5"), quantity=Decimal("10")),)
    result = executable_vwap(levels, Decimal("5"), side="BUY")
    assert result.sufficient is True
    assert result.vwap == Decimal("0.5")


def test_crossed_book_rejected() -> None:
    with pytest.raises(ValueError, match="crossed"):
        BookSnapshot(
            instrument_id=InstrumentId("x"),
            ts_event=TS,
            bids=(BookLevel(price=Decimal("0.6"), quantity=Decimal("1")),),
            asks=(BookLevel(price=Decimal("0.5"), quantity=Decimal("1")),),
        )
