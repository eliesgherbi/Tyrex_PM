"""Adapter normalization fixture tests."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.adapters.binance.normalize import normalize_trade_message
from tyrex_pm.adapters.polymarket.normalize import (
    normalize_book_message,
    normalize_price_change,
    normalize_tick_size_change,
)
from tyrex_pm.core.book_events import BookSide


TS = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def test_normalize_book_and_delta() -> None:
    snap = normalize_book_message(
        {
            "event_type": "book",
            "asset_id": "tok-1",
            "timestamp": 1784203200000,
            "bids": [{"price": "0.4", "size": "10"}],
            "asks": [{"price": "0.6", "size": "10"}],
            "hash": "abc",
        },
        ts_received=TS,
    )
    assert snap.book.instrument_id.value == "tok-1"
    assert snap.venue_hash == "abc"

    delta = normalize_price_change(
        {
            "event_type": "price_change",
            "timestamp": 1784203201000,
            "price_changes": [
                {"asset_id": "tok-1", "side": "SELL", "price": "0.59", "size": "3"},
                {"asset_id": "tok-1", "side": "BUY", "price": "0.4", "size": "0"},
            ],
        },
        ts_received=TS,
    )
    assert len(delta.changes) == 2
    assert delta.changes[0].side is BookSide.ASK
    assert delta.changes[1].size == Decimal("0")


def test_normalize_tick_size() -> None:
    evt = normalize_tick_size_change(
        {
            "event_type": "tick_size_change",
            "asset_id": "tok-1",
            "timestamp": 1784203200000,
            "old_tick_size": "0.01",
            "new_tick_size": "0.001",
        },
        ts_received=TS,
    )
    assert evt.new_tick_size == Decimal("0.001")


def test_normalize_binance_trade() -> None:
    evt = normalize_trade_message(
        {"s": "btcusdt", "p": "65000.5", "T": 1784203200000},
        ts_received=TS,
    )
    assert evt.reference.symbol == "BTCUSDT"
    assert evt.reference.price == Decimal("65000.5")
