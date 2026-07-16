"""External BTC ingest loop for record-only mode (M2B.2)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from tyrex_pm.core.events import MarketEvent
from tyrex_pm.venue.binance_data.normalize import (
    EXTERNAL_BTC_MARKET_ID,
    build_clock_sync_event,
    build_external_btc_tick_event,
    classify_binance_stream,
    normalize_agg_trade,
    normalize_book_ticker,
)
from tyrex_pm.venue.binance_data.ws_client import BinanceDataWsClient

log = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def run_external_btc_ingest(
    *,
    symbol: str,
    streams: list[str] | tuple[str, ...],
    venue: str,
    stop: asyncio.Event,
    on_event_emitted: Callable[[MarketEvent], Any],
    reconnect_backoff_s: float = 3.0,
    clock_sync_interval_s: float = 60.0,
) -> None:
    """Connect to public Binance WS, normalize ticks, and emit MarketEvents."""
    client = BinanceDataWsClient(symbol=symbol, streams=streams)
    last_source_ts: datetime | None = None

    async def _emit(event: MarketEvent) -> None:
        maybe = on_event_emitted(event)
        if asyncio.iscoroutine(maybe):
            await maybe

    async def _emit_clock_sync(recv_ts: datetime, source_ts: datetime | None) -> None:
        await _emit(
            build_clock_sync_event(
                symbol=symbol,
                venue=venue,
                recv_ts=recv_ts,
                source_ts=source_ts,
                market_id=EXTERNAL_BTC_MARKET_ID,
            )
        )

    async def _clock_sync_loop() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(1.0, clock_sync_interval_s))
                return
            except asyncio.TimeoutError:
                await _emit_clock_sync(_utc_now(), last_source_ts)

    async def _on_message(stream_name: str | None, data: dict[str, Any]) -> None:
        nonlocal last_source_ts
        recv_ts = _utc_now()
        kind = classify_binance_stream(stream_name, data)
        if kind == "bookTicker":
            payload = normalize_book_ticker(data, symbol=symbol, venue=venue)
            cursor = str(data.get("u", stream_name or "bookTicker"))
            event = build_external_btc_tick_event(
                payload,
                recv_ts=recv_ts,
                venue_cursor=cursor,
                source_ts=None,
            )
        elif kind == "aggTrade":
            payload = normalize_agg_trade(data, symbol=symbol, venue=venue)
            trade_ts_raw = data.get("T") or data.get("E")
            source_ts = None
            if trade_ts_raw is not None:
                try:
                    source_ts = datetime.fromtimestamp(int(trade_ts_raw) / 1000.0, tz=timezone.utc)
                    last_source_ts = source_ts
                except (TypeError, ValueError):
                    source_ts = None
            event = build_external_btc_tick_event(
                payload,
                recv_ts=recv_ts,
                venue_cursor=str(data.get("a", stream_name or "aggTrade")),
                source_ts=source_ts,
            )
        else:
            return
        await _emit(event)

    await _emit_clock_sync(_utc_now(), last_source_ts)
    clock_task = asyncio.create_task(_clock_sync_loop())
    try:
        await client.run(stop=stop, on_message=_on_message, reconnect_backoff_s=reconnect_backoff_s)
    finally:
        clock_task.cancel()
        try:
            await clock_task
        except asyncio.CancelledError:
            pass
