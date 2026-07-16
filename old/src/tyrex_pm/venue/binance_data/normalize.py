"""Normalize Binance public WS messages into canonical external BTC payloads (M2B.2)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from tyrex_pm.core.events import EventType, MarketEvent, compute_event_id, compute_payload_digest

EXTERNAL_BTC_MARKET_ID = "external/btc_binance"


def _decimal_mid(bid: str, ask: str) -> str:
    b = Decimal(str(bid))
    a = Decimal(str(ask))
    return str((b + a) / Decimal("2"))


def _ms_to_dt(ms: int | float | str | None) -> datetime | None:
    if ms is None:
        return None
    try:
        value = int(ms)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)


def normalize_book_ticker(
    raw: dict[str, Any],
    *,
    symbol: str,
    venue: str = "binance",
    stream: str = "bookTicker",
) -> dict[str, Any]:
    bid = str(raw.get("b", ""))
    ask = str(raw.get("a", ""))
    payload: dict[str, Any] = {
        "source": venue,
        "symbol": str(raw.get("s") or symbol).upper(),
        "stream": stream,
        "bid": bid,
        "ask": ask,
        "mid": _decimal_mid(bid, ask) if bid and ask else None,
        "source_event_ts": None,
        "raw": raw,
    }
    return payload


def normalize_agg_trade(
    raw: dict[str, Any],
    *,
    symbol: str,
    venue: str = "binance",
    stream: str = "aggTrade",
) -> dict[str, Any]:
    trade_ts = _ms_to_dt(raw.get("T") or raw.get("E"))
    payload: dict[str, Any] = {
        "source": venue,
        "symbol": str(raw.get("s") or symbol).upper(),
        "stream": stream,
        "price": str(raw.get("p", "")),
        "quantity": str(raw.get("q", "")),
        "trade_id": str(raw.get("a", "")),
        "source_event_ts": trade_ts.isoformat() if trade_ts is not None else None,
        "raw": raw,
    }
    return payload


def build_external_btc_tick_event(
    payload: dict[str, Any],
    *,
    recv_ts: datetime,
    venue_cursor: str | None = None,
    source_ts: datetime | None = None,
    market_id: str = EXTERNAL_BTC_MARKET_ID,
) -> MarketEvent:
    digest = compute_payload_digest(payload)
    stream = str(payload.get("stream", ""))
    cursor = venue_cursor or str(payload.get("trade_id") or payload.get("raw", {}).get("u") or stream)
    parsed_source = source_ts
    if parsed_source is None:
        raw_ts = payload.get("source_event_ts")
        if isinstance(raw_ts, str):
            parsed_source = datetime.fromisoformat(raw_ts)
    return MarketEvent(
        event_id=compute_event_id(
            event_type=EventType.EXTERNAL_BTC_TICK.value,
            token_id=None,
            venue_cursor=cursor,
            source_ts=parsed_source,
            payload_digest=digest,
        ),
        event_type=EventType.EXTERNAL_BTC_TICK,
        market_id=market_id,
        token_id=None,
        venue_cursor=cursor,
        source_ts=parsed_source,
        recv_ts=recv_ts,
        payload=payload,
    )


def build_clock_sync_event(
    *,
    symbol: str,
    venue: str,
    recv_ts: datetime,
    source_ts: datetime | None,
    market_id: str = EXTERNAL_BTC_MARKET_ID,
) -> MarketEvent:
    latency_ms = None
    if source_ts is not None:
        latency_ms = round((recv_ts - source_ts).total_seconds() * 1000.0, 3)
    payload = {
        "source": venue,
        "symbol": symbol.upper(),
        "local_recv_ts": recv_ts.astimezone(timezone.utc).isoformat(),
        "source_ts": source_ts.astimezone(timezone.utc).isoformat() if source_ts is not None else None,
        "latency_ms": latency_ms,
    }
    digest = compute_payload_digest(payload)
    source_ms = int(source_ts.timestamp() * 1000) if source_ts is not None else 0
    cursor = f"clock_sync|{source_ms}|{int(recv_ts.timestamp() * 1000)}"
    return MarketEvent(
        event_id=compute_event_id(
            event_type=EventType.CLOCK_SYNC.value,
            token_id=None,
            venue_cursor=cursor,
            source_ts=source_ts,
            payload_digest=digest,
        ),
        event_type=EventType.CLOCK_SYNC,
        market_id=market_id,
        token_id=None,
        venue_cursor=cursor,
        source_ts=source_ts,
        recv_ts=recv_ts,
        payload=payload,
    )


def classify_binance_stream(stream_name: str | None, data: dict[str, Any]) -> str | None:
    name = (stream_name or str(data.get("e", ""))).lower()
    if "bookticker" in name or ("b" in data and "a" in data and "e" not in data):
        return "bookTicker"
    if "aggtrade" in name or data.get("e") == "aggTrade":
        return "aggTrade"
    return None
