"""Unit tests for public book summarization (v1.01)."""

from tyrex_pm.data.book_check import (
    summarize_book_sides,
    tick_size_matches_instrument,
)


def test_summarize_empty_book():
    st, d = summarize_book_sides(None, None)
    assert st == "empty_book"
    assert "no_bids" in d


def test_summarize_with_liquidity():
    st, d = summarize_book_sides([{"price": "0.5"}], [{"price": "0.6"}])
    assert st == "ok_liquidity"
    assert "bids=1" in d and "asks=1" in d


def test_tick_match():
    assert tick_size_matches_instrument("0.001", "0.001") is True
    assert tick_size_matches_instrument("0.01", "0.001") is False
