"""Polymarket market-channel extended event tests (M2B.3-A)."""

from __future__ import annotations

from datetime import datetime, timezone

from tyrex_pm.core.events import EventType, event_from_dict, event_to_dict
from tyrex_pm.ingestion.event_factory import build_market_event_from_ws

_RECV = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


def _build(msg: dict):
    return build_market_event_from_ws(
        msg,
        received_ts=_RECV,
        connection_id="conn-1",
        local_counter=1,
        market_id="btc_5m_test",
    )


def test_last_trade_price_event() -> None:
    msg = {
        "event_type": "last_trade_price",
        "asset_id": "tok_yes",
        "market": "0xabc",
        "price": "0.456",
        "size": "10",
        "side": "BUY",
        "fee_rate_bps": "0",
        "timestamp": "1750428146322",
    }
    event = _build(msg)
    assert event is not None
    assert event.event_type == EventType.LAST_TRADE_PRICE
    assert event.payload["raw"] == msg
    restored = event_from_dict(event_to_dict(event))
    assert restored.event_id == event.event_id
    assert restored.payload["raw"]["price"] == "0.456"


def test_tick_size_change_event() -> None:
    msg = {
        "event_type": "tick_size_change",
        "asset_id": "tok_yes",
        "market": "0xabc",
        "old_tick_size": "0.01",
        "new_tick_size": "0.001",
        "timestamp": "100000000",
    }
    event = _build(msg)
    assert event is not None
    assert event.event_type == EventType.TICK_SIZE_CHANGE
    assert event.payload["raw"]["new_tick_size"] == "0.001"


def test_best_bid_ask_event() -> None:
    msg = {
        "event_type": "best_bid_ask",
        "asset_id": "tok_yes",
        "market": "0xabc",
        "best_bid": "0.73",
        "best_ask": "0.77",
        "spread": "0.04",
        "timestamp": "1766789469958",
    }
    event = _build(msg)
    assert event is not None
    assert event.event_type == EventType.BEST_BID_ASK
    assert event.payload["raw"]["spread"] == "0.04"


def test_new_market_event() -> None:
    msg = {
        "event_type": "new_market",
        "id": "1031769",
        "slug": "btc-updown-5m-1780000000",
        "market": "0xabc",
        "outcomes": ["Up", "Down"],
        "timestamp": "1766790415550",
    }
    event = _build(msg)
    assert event is not None
    assert event.event_type == EventType.NEW_MARKET
    assert event.payload["raw"]["slug"] == "btc-updown-5m-1780000000"


def test_market_resolved_event() -> None:
    msg = {
        "event_type": "market_resolved",
        "id": "1031769",
        "market": "0xabc",
        "winning_asset_id": "tok_yes",
        "winning_outcome": "Up",
        "timestamp": "1766790415550",
    }
    event = _build(msg)
    assert event is not None
    assert event.event_type == EventType.MARKET_RESOLVED
    assert event.payload["raw"]["winning_outcome"] == "Up"


def test_unknown_event_returns_none() -> None:
    assert _build({"event_type": "totally_unknown"}) is None
