"""RTDS reference price event tests (M2B.3-A)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from tyrex_pm.core.events import EventType, event_from_dict, event_to_dict
from tyrex_pm.ingestion.price_to_beat_tracker import PriceToBeatTracker
from tyrex_pm.venue.polymarket_rtds.normalize import (
    build_price_to_beat_observed_event,
    build_reference_price_subscription,
    build_reference_price_tick_event,
    normalize_reference_price_tick,
    normalize_reference_price_ticks,
)
from tyrex_pm.venue.polymarket_rtds.ws_client import build_subscribe_message

CHAINLINK_MSG = {
    "topic": "crypto_prices_chainlink",
    "type": "update",
    "timestamp": 1753314088421,
    "payload": {
        "symbol": "btc/usd",
        "timestamp": 1780000000500,
        "value": 109812.50,
    },
}

CHAINLINK_SNAPSHOT_MSG = {
    "topic": "crypto_prices_chainlink",
    "type": "subscribe",
    "timestamp": 1753314064237,
    "payload": {
        "symbol": "btc/usd",
        "data": [
            {"timestamp": 1780000000100, "value": 109800.0},
            {"timestamp": 1780000000200, "value": 109801.0},
        ],
    },
}


def _recv() -> datetime:
    return datetime(2026, 7, 3, 19, 0, 0, tzinfo=timezone.utc)


def test_reference_price_subscription_payload_uses_type_not_msg_type() -> None:
    subscription = build_reference_price_subscription("chainlink", "btc/usd")
    assert "type" in subscription
    assert "msg_type" not in subscription
    assert subscription["topic"] == "crypto_prices_chainlink"
    assert subscription["type"] in {"*", "update"}
    assert isinstance(subscription["filters"], str)
    assert json.loads(subscription["filters"])["symbol"] == "btc/usd"

    wrapped = build_subscribe_message(
        topic=subscription["topic"],
        msg_type=subscription["type"],
        filters=subscription["filters"],
    )
    sub = wrapped["subscriptions"][0]
    assert sub["type"] == "*"
    assert "msg_type" not in sub


def test_reference_price_historical_snapshot_normalization() -> None:
    payloads = normalize_reference_price_ticks(CHAINLINK_SNAPSHOT_MSG, feed="chainlink")
    assert len(payloads) == 2
    assert payloads[0]["value"] == "109800.0"
    assert payloads[1]["value"] == "109801.0"
    assert payloads[0]["type"] == "subscribe"
    events = [
        build_reference_price_tick_event(payload, recv_ts=_recv())
        for payload in payloads
    ]
    assert events[0].event_id != events[1].event_id


def test_price_to_beat_final_reference_and_direction(tmp_path) -> None:
    tracker = PriceToBeatTracker(max_lag_ms=5000.0, chainlink_log_path=tmp_path / "empty.jsonl")
    tracker.register_market(
        market_id="btc_5m_final",
        event_start_ts=1780000000.0,
        event_end_ts=1780000300.0,
    )
    start_msg = {
        "topic": "crypto_prices_chainlink",
        "type": "update",
        "timestamp": 1780000000500,
        "payload": {"symbol": "btc/usd", "timestamp": 1780000000500, "value": 109812.50},
    }
    end_msg = {
        "topic": "crypto_prices_chainlink",
        "type": "update",
        "timestamp": 1780000300100,
        "payload": {"symbol": "btc/usd", "timestamp": 1780000300100, "value": 109820.00},
    }
    recv = _recv()
    start_event = build_reference_price_tick_event(
        normalize_reference_price_tick(start_msg, feed="chainlink"),
        recv_ts=recv,
    )
    end_event = build_reference_price_tick_event(
        normalize_reference_price_tick(end_msg, feed="chainlink"),
        recv_ts=recv,
    )
    first = tracker.on_reference_tick(start_event)
    assert len(first) == 1
    assert first[0].payload["status"] == "observed"
    assert first[0].payload["price_to_beat"] == "109812.5"
    assert first[0].payload["price_to_beat_source"] == "polymarket_rtds_chainlink"
    assert first[0].payload["price_to_beat_lag_ms"] == 500.0

    second = tracker.on_reference_tick(end_event)
    assert len(second) == 1
    assert second[0].payload["status"] == "complete"
    assert second[0].payload["final_reference_price"] == "109820.0"
    assert second[0].payload["direction_vs_price_to_beat"] == "up"


def test_reference_price_tick_ignores_other_feed_topic() -> None:
    binance_msg = {
        "topic": "crypto_prices",
        "type": "update",
        "timestamp": 1753314088421,
        "payload": {"symbol": "btcusdt", "timestamp": 1780000000500, "value": 67234.50},
    }
    assert normalize_reference_price_tick(binance_msg, feed="chainlink") is None
    assert normalize_reference_price_ticks(binance_msg, feed="chainlink") == []


def test_reference_price_tick_normalization() -> None:
    payload = normalize_reference_price_tick(CHAINLINK_MSG, feed="chainlink")
    assert payload is not None
    assert payload["feed"] == "chainlink"
    assert payload["symbol"] == "btc/usd"
    assert payload["value"] == "109812.5"
    assert payload["raw"] == CHAINLINK_MSG


def test_reference_price_tick_round_trip() -> None:
    payload = normalize_reference_price_tick(CHAINLINK_MSG, feed="chainlink")
    event = build_reference_price_tick_event(payload, recv_ts=_recv())
    assert event.event_type == EventType.REFERENCE_PRICE_TICK
    restored = event_from_dict(event_to_dict(event))
    assert restored.payload["raw"] == CHAINLINK_MSG
    assert restored.payload["value"] == "109812.5"


def test_price_to_beat_derived_from_first_tick_after_start(tmp_path) -> None:
    tracker = PriceToBeatTracker(max_lag_ms=5000.0, chainlink_log_path=tmp_path / "empty.jsonl")
    tracker.register_market(
        market_id="btc_5m_20260703_1900",
        event_start_ts=1780000000.0,
        event_end_ts=1780000300.0,
    )
    payload = normalize_reference_price_tick(CHAINLINK_MSG, feed="chainlink")
    ref_event = build_reference_price_tick_event(payload, recv_ts=_recv())
    derived = tracker.on_reference_tick(ref_event)
    assert len(derived) == 1
    assert derived[0].event_type == EventType.PRICE_TO_BEAT_OBSERVED
    assert derived[0].payload["price_to_beat"] == "109812.5"
    assert derived[0].payload["status"] == "observed"


def test_price_to_beat_missing_when_tick_too_late() -> None:
    tracker = PriceToBeatTracker(max_lag_ms=100.0)
    tracker.register_market(
        market_id="btc_5m_late",
        event_start_ts=1780000000.0,
        event_end_ts=1780000300.0,
    )
    late_msg = {
        "topic": "crypto_prices_chainlink",
        "type": "update",
        "timestamp": 1780000010000,
        "payload": {"symbol": "btc/usd", "timestamp": 1780000010000, "value": 110000.0},
    }
    payload = normalize_reference_price_tick(late_msg, feed="chainlink")
    ref_event = build_reference_price_tick_event(payload, recv_ts=_recv())
    tracker.on_reference_tick(ref_event)
    derived = tracker.flush_missing()
    assert any(e.payload["status"] == "missing" for e in derived)


def test_price_to_beat_observed_event_round_trip() -> None:
    event = build_price_to_beat_observed_event(
        market_id="btc_5m_test",
        event_start_ts=1780000000.0,
        event_end_ts=1780000300.0,
        price_to_beat="109812.50",
        price_to_beat_ts=_recv(),
        price_to_beat_source="polymarket_rtds_chainlink",
        price_to_beat_lag_ms=50.0,
        raw_reference_event_id="abc",
        status="observed",
        recv_ts=_recv(),
    )
    restored = event_from_dict(event_to_dict(event))
    assert restored.event_type == EventType.PRICE_TO_BEAT_OBSERVED
    assert restored.payload["price_to_beat"] == "109812.50"
