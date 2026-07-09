"""Normalize Polymarket RTDS crypto price messages (M2B.3-A)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from tyrex_pm.core.events import EventType, MarketEvent, compute_event_id, compute_payload_digest

REFERENCE_PRICES_MARKET_ID = "external/polymarket_rtds_chainlink"

_FEED_TOPIC_MAP = {
    "chainlink": "crypto_prices_chainlink",
    "binance": "crypto_prices",
}


def feed_topic(feed: str) -> str:
    key = str(feed).strip().lower()
    topic = _FEED_TOPIC_MAP.get(key)
    if topic is None:
        raise ValueError(f"unsupported reference price feed: {feed!r}")
    return topic


def symbol_filter(feed: str, symbol: str) -> str:
    sym = str(symbol).strip()
    if feed_topic(feed) == "crypto_prices_chainlink":
        return json_filter_symbol(sym)
    return sym.replace("/", "").lower()


def json_filter_symbol(symbol: str) -> str:
    return '{"symbol":"' + str(symbol).strip().lower() + '"}'


def build_reference_price_subscription(
    feed: str,
    symbol: str,
    *,
    subscription_type: str = "*",
) -> dict[str, Any]:
    """Build one RTDS subscription entry for a reference-price feed/symbol pair."""
    return {
        "topic": feed_topic(feed),
        "type": subscription_type,
        "filters": symbol_filter(feed, symbol),
    }


def _ms_to_dt(ms: int | float | str | None) -> datetime | None:
    if ms is None:
        return None
    try:
        value = int(ms)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)


def classify_rtds_message(msg: dict[str, Any]) -> str | None:
    topic = str(msg.get("topic") or "").strip()
    if topic in {"crypto_prices_chainlink", "crypto_prices"}:
        return topic
    return None


def _normalize_single_reference_price_payload(
    *,
    msg: dict[str, Any],
    topic: str,
    feed_name: str,
    venue: str,
    symbol: str,
    value: Any,
    item_timestamp: int | float | str | None,
    msg_type: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    source_ts = _ms_to_dt(item_timestamp or msg.get("timestamp"))
    return {
        "source": venue,
        "feed": feed_name,
        "symbol": symbol.lower(),
        "value": str(value),
        "source_event_ts": source_ts.isoformat() if source_ts is not None else None,
        "topic": topic,
        "type": msg_type,
        "raw": msg,
    }


def normalize_reference_price_ticks(
    msg: dict[str, Any],
    *,
    feed: str,
    venue: str = "polymarket_rtds",
) -> list[dict[str, Any]]:
    """Normalize one RTDS message into zero or more reference-price tick payloads."""
    topic = classify_rtds_message(msg)
    if topic is None:
        return []
    expected_topic = feed_topic(feed)
    if topic != expected_topic:
        return []
    payload = msg.get("payload")
    if not isinstance(payload, dict):
        return []
    msg_type = str(msg.get("type") or "")
    feed_name = "chainlink" if topic == "crypto_prices_chainlink" else "binance"
    symbol = str(payload.get("symbol") or "").lower()
    out: list[dict[str, Any]] = []

    data = payload.get("data")
    if isinstance(data, list) and msg_type in {"subscribe", "snapshot"}:
        for item in data:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_single_reference_price_payload(
                msg=msg,
                topic=topic,
                feed_name=feed_name,
                venue=venue,
                symbol=symbol,
                value=item.get("value"),
                item_timestamp=item.get("timestamp") or payload.get("timestamp"),
                msg_type=msg_type,
            )
            if normalized is not None:
                out.append(normalized)
        return out

    normalized = _normalize_single_reference_price_payload(
        msg=msg,
        topic=topic,
        feed_name=feed_name,
        venue=venue,
        symbol=symbol,
        value=payload.get("value"),
        item_timestamp=payload.get("timestamp") or msg.get("timestamp"),
        msg_type=msg_type,
    )
    if normalized is not None:
        out.append(normalized)
    return out


def normalize_reference_price_tick(
    msg: dict[str, Any],
    *,
    feed: str,
    venue: str = "polymarket_rtds",
) -> dict[str, Any] | None:
    ticks = normalize_reference_price_ticks(msg, feed=feed, venue=venue)
    return ticks[0] if ticks else None


def build_reference_price_tick_event(
    payload: dict[str, Any],
    *,
    recv_ts: datetime,
    source_ts: datetime | None = None,
    market_id: str = REFERENCE_PRICES_MARKET_ID,
) -> MarketEvent:
    parsed_source = source_ts
    if parsed_source is None:
        raw_ts = payload.get("source_event_ts")
        if isinstance(raw_ts, str):
            parsed_source = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            if parsed_source.tzinfo is None:
                parsed_source = parsed_source.replace(tzinfo=timezone.utc)
    latency_ms = None
    if parsed_source is not None:
        latency_ms = round((recv_ts - parsed_source).total_seconds() * 1000.0, 3)
    payload = dict(payload)
    payload["latency_ms"] = latency_ms
    digest = compute_payload_digest(payload)
    cursor = f"{payload.get('feed')}|{payload.get('symbol')}|{payload.get('source_event_ts')}|{payload.get('value')}"
    return MarketEvent(
        event_id=compute_event_id(
            event_type=EventType.REFERENCE_PRICE_TICK.value,
            token_id=None,
            venue_cursor=cursor,
            source_ts=parsed_source,
            payload_digest=digest,
        ),
        event_type=EventType.REFERENCE_PRICE_TICK,
        market_id=market_id,
        token_id=None,
        venue_cursor=cursor,
        source_ts=parsed_source,
        recv_ts=recv_ts,
        payload=payload,
    )


def build_price_to_beat_observed_event(
    *,
    market_id: str,
    event_start_ts: float,
    event_end_ts: float,
    price_to_beat: str | None,
    price_to_beat_ts: datetime | None,
    price_to_beat_source: str,
    price_to_beat_lag_ms: float | None,
    raw_reference_event_id: str | None,
    status: str,
    recv_ts: datetime,
    final_reference_price: str | None = None,
    final_reference_price_ts: datetime | None = None,
    final_reference_lag_ms: float | None = None,
    direction_vs_price_to_beat: str | None = None,
) -> MarketEvent:
    payload: dict[str, Any] = {
        "market_id": market_id,
        "event_start_ts": event_start_ts,
        "event_end_ts": event_end_ts,
        "price_to_beat": price_to_beat,
        "price_to_beat_ts": price_to_beat_ts.isoformat() if price_to_beat_ts is not None else None,
        "price_to_beat_source": price_to_beat_source,
        "price_to_beat_lag_ms": price_to_beat_lag_ms,
        "raw_reference_event_id": raw_reference_event_id,
        "status": status,
        "final_reference_price": final_reference_price,
        "final_reference_price_ts": (
            final_reference_price_ts.isoformat() if final_reference_price_ts is not None else None
        ),
        "final_reference_lag_ms": final_reference_lag_ms,
        "direction_vs_price_to_beat": direction_vs_price_to_beat,
    }
    digest = compute_payload_digest(payload)
    cursor = f"ptb|{market_id}|{status}|{price_to_beat or 'missing'}"
    return MarketEvent(
        event_id=compute_event_id(
            event_type=EventType.PRICE_TO_BEAT_OBSERVED.value,
            token_id=None,
            venue_cursor=cursor,
            source_ts=price_to_beat_ts,
            payload_digest=digest,
        ),
        event_type=EventType.PRICE_TO_BEAT_OBSERVED,
        market_id=market_id,
        token_id=None,
        venue_cursor=cursor,
        source_ts=price_to_beat_ts,
        recv_ts=recv_ts,
        payload=payload,
    )
