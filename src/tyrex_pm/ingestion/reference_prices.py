"""Polymarket RTDS reference price ingest for record-only mode (M2B.3-A)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from tyrex_pm.core.events import MarketEvent
from tyrex_pm.ingestion.price_to_beat_tracker import PriceToBeatTracker
from tyrex_pm.venue.polymarket_rtds.normalize import (
    build_reference_price_subscription,
    build_reference_price_tick_event,
    normalize_reference_price_ticks,
)
from tyrex_pm.venue.polymarket_rtds.ws_client import PolymarketRtdsWsClient

log = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def run_reference_prices_ingest(
    *,
    feeds: list[str] | tuple[str, ...],
    symbols: list[str] | tuple[str, ...],
    venue: str,
    stop: asyncio.Event,
    on_event_emitted: Callable[[MarketEvent], Any],
    price_to_beat_tracker: PriceToBeatTracker | None = None,
    reconnect_backoff_s: float = 3.0,
) -> None:
    """Connect to Polymarket RTDS, normalize crypto prices, emit MarketEvents."""
    subscriptions: list[dict[str, Any]] = [
        build_reference_price_subscription(feed, symbol)
        for feed in feeds
        for symbol in symbols
    ]
    client = PolymarketRtdsWsClient(subscriptions=subscriptions)

    async def _emit(event: MarketEvent) -> None:
        maybe = on_event_emitted(event)
        if asyncio.iscoroutine(maybe):
            await maybe

    async def _on_message(msg: dict[str, Any]) -> None:
        recv_ts = _utc_now()
        for feed in feeds:
            payloads = normalize_reference_price_ticks(msg, feed=feed, venue=venue)
            if not payloads:
                continue
            for payload in payloads:
                event = build_reference_price_tick_event(payload, recv_ts=recv_ts)
                await _emit(event)
                if price_to_beat_tracker is not None:
                    for derived in price_to_beat_tracker.on_reference_tick(event):
                        await _emit(derived)
            return

    flush_task = asyncio.create_task(_flush_missing_loop(price_to_beat_tracker, stop, _emit))
    try:
        await client.run(stop=stop, on_message=_on_message, reconnect_backoff_s=reconnect_backoff_s)
    finally:
        flush_task.cancel()
        try:
            await flush_task
        except asyncio.CancelledError:
            pass
        if price_to_beat_tracker is not None:
            for derived in price_to_beat_tracker.flush_missing():
                await _emit(derived)


async def _flush_missing_loop(
    tracker: PriceToBeatTracker | None,
    stop: asyncio.Event,
    emit: Callable[[MarketEvent], Any],
) -> None:
    if tracker is None:
        await stop.wait()
        return
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=5.0)
            break
        except asyncio.TimeoutError:
            for derived in tracker.flush_missing():
                maybe = emit(derived)
                if asyncio.iscoroutine(maybe):
                    await maybe
