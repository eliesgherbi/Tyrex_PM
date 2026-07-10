"""Live signal feed runtime — Binance + RTDS ingest into SignalStateStore (A0.2)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from tyrex_pm.core.events import EventType, MarketEvent
from tyrex_pm.ingestion.price_to_beat_tracker import PriceToBeatTracker
from tyrex_pm.runtime.config import AppConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.signal_feed_facts import (
    build_basis_computed_payload,
    build_price_to_beat_observed_payload,
    build_signal_feed_health_from_snapshot,
)
from tyrex_pm.state.signal_state_store import SignalStateStore

log = logging.getLogger(__name__)


def _decimal_price(raw: object) -> Decimal | None:
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _apply_external_btc_event(store: SignalStateStore, event: MarketEvent) -> None:
    payload = event.payload or {}
    stream = str(payload.get("stream") or "aggTrade")
    recv_ts = event.recv_ts
    source_ts = event.source_ts
    price: Decimal | None = None
    if stream == "bookTicker":
        price = _decimal_price(payload.get("mid"))
        if price is None:
            bid = _decimal_price(payload.get("bid"))
            ask = _decimal_price(payload.get("ask"))
            if bid is not None and ask is not None:
                price = (bid + ask) / Decimal("2")
    else:
        price = _decimal_price(payload.get("price"))
    if price is None:
        return
    store.update_binance(price, source_ts=source_ts, recv_ts=recv_ts, stream=stream)
    store.mark_binance_connected(connected=True)


def _apply_reference_tick_event(store: SignalStateStore, event: MarketEvent) -> None:
    payload = event.payload or {}
    if str(payload.get("feed") or "").lower() != "chainlink":
        return
    price = _decimal_price(payload.get("value"))
    if price is None:
        return
    store.update_chainlink(price, source_ts=event.source_ts, recv_ts=event.recv_ts)
    store.mark_chainlink_connected(connected=True)


def _apply_price_to_beat_event(store: SignalStateStore, event: MarketEvent) -> None:
    payload = event.payload or {}
    raw_k = payload.get("price_to_beat")
    price = _decimal_price(raw_k)
    lag_raw = payload.get("price_to_beat_lag_ms")
    lag_ms: float | None = None
    if lag_raw not in (None, ""):
        try:
            lag_ms = float(lag_raw)
        except (TypeError, ValueError):
            lag_ms = None
    observed_ts = event.source_ts or event.recv_ts
    store.update_price_to_beat(
        price,
        status=str(payload.get("status") or "pending"),
        observed_ts=observed_ts,
        lag_ms=lag_ms,
    )


def apply_market_event_to_signal_store(store: SignalStateStore, event: MarketEvent) -> None:
    """Route a canonical MarketEvent into the signal state store."""
    if event.event_type == EventType.EXTERNAL_BTC_TICK:
        _apply_external_btc_event(store, event)
    elif event.event_type == EventType.REFERENCE_PRICE_TICK:
        _apply_reference_tick_event(store, event)
    elif event.event_type == EventType.PRICE_TO_BEAT_OBSERVED:
        _apply_price_to_beat_event(store, event)


@dataclass
class SignalFeedRuntimeState:
    """Handles for background signal feed tasks."""

    binance_task: asyncio.Task[None] | None = None
    reference_task: asyncio.Task[None] | None = None
    binance_stop: asyncio.Event | None = None
    reference_stop: asyncio.Event | None = None
    price_to_beat_tracker: PriceToBeatTracker | None = None
    market_id: str | None = None
    _last_basis_key: tuple[str | None, str | None, str] | None = field(default=None, repr=False)


def _should_emit_basis(
    state: SignalFeedRuntimeState,
    snap,
) -> bool:
    key = (
        str(snap.basis_bps) if snap.basis_bps is not None else None,
        str(snap.basis_status),
        snap.chainlink_freshness,
    )
    if state._last_basis_key == key:
        return False
    state._last_basis_key = key
    return snap.basis_bps is not None or snap.basis_status != "missing"


def build_feed_health_payloads(store: SignalStateStore) -> list[dict[str, Any]]:
    snap = store.snapshot()
    return [
        build_signal_feed_health_from_snapshot(snap, feed="binance"),
        build_signal_feed_health_from_snapshot(snap, feed="chainlink"),
    ]


async def start_signal_feeds(
    *,
    coord: RuntimeCoordinator,
    app: AppConfig,
    stop: asyncio.Event,
    market_id: str | None = None,
    event_start_ts: float | None = None,
    event_end_ts: float | None = None,
    on_health: Callable[[dict[str, Any]], Any] | None = None,
) -> SignalFeedRuntimeState:
    """Start Binance + RTDS background ingest tasks updating ``coord.signal_state``."""
    from tyrex_pm.runtime.time_authority import mark_feeds_started

    mark_feeds_started(caller="start_signal_feeds")
    feeds = app.runtime.signal_feeds
    ext = app.runtime.external_btc
    ref = app.runtime.reference_prices

    if not feeds.enabled:
        return SignalFeedRuntimeState()

    if coord.signal_state is None:
        coord.signal_state = SignalStateStore(
            binance_max_age_ms=feeds.binance_max_age_ms,
            chainlink_max_age_ms=feeds.chainlink_max_age_ms,
            book_ticker_fallback_stale_ms=feeds.book_ticker_fallback_stale_ms,
            ptb_late_threshold_ms=feeds.ptb_late_threshold_ms,
        )
    store: SignalStateStore = coord.signal_state
    state = SignalFeedRuntimeState(market_id=market_id)

    async def _on_event(event: MarketEvent) -> None:
        apply_market_event_to_signal_store(store, event)
        if on_health is None:
            return
        snap = store.snapshot()
        if _should_emit_basis(state, snap):
            basis_payload = build_basis_computed_payload(snap)
            maybe = on_health({"kind": "basis_computed", **basis_payload})
            if asyncio.iscoroutine(maybe):
                await maybe
        if event.event_type == EventType.PRICE_TO_BEAT_OBSERVED and market_id:
            ptb_payload = build_price_to_beat_observed_payload(
                market_id=market_id,
                price_to_beat=str(snap.price_to_beat) if snap.price_to_beat is not None else None,
                ptb_status=snap.ptb_status,
                ptb_lag_ms=snap.ptb_lag_ms,
                event_start_ts=event_start_ts,
                event_end_ts=event_end_ts,
            )
            maybe = on_health({"kind": "price_to_beat_observed", **ptb_payload})
            if asyncio.iscoroutine(maybe):
                await maybe

    if ext.enabled and state.binance_task is None:
        from tyrex_pm.ingestion.external_btc import run_external_btc_ingest

        binance_stop = asyncio.Event()

        async def _binance_wrapper() -> None:
            local_stop = asyncio.Event()

            async def _relay() -> None:
                await stop.wait()
                local_stop.set()
                binance_stop.set()

            relay = asyncio.create_task(_relay())
            try:
                await run_external_btc_ingest(
                    symbol=ext.symbol,
                    streams=list(ext.streams),
                    venue=ext.venue,
                    stop=local_stop,
                    on_event_emitted=_on_event,
                    reconnect_backoff_s=ext.reconnect_backoff_s,
                    clock_sync_interval_s=ext.clock_sync_interval_s,
                )
            finally:
                store.mark_binance_connected(connected=False)
                relay.cancel()
                try:
                    await relay
                except asyncio.CancelledError:
                    pass

        state.binance_stop = binance_stop
        state.binance_task = asyncio.create_task(_binance_wrapper())
        log.info("signal_feed_runtime: Binance ingest started (%s)", ext.symbol)

    if ref.enabled and state.reference_task is None:
        from tyrex_pm.ingestion.reference_prices import run_reference_prices_ingest

        ref_stop = asyncio.Event()
        tracker: PriceToBeatTracker | None = None
        if ref.emit_price_to_beat and market_id and event_start_ts is not None and event_end_ts is not None:
            tracker = PriceToBeatTracker(max_lag_ms=ref.price_to_beat_max_lag_ms)
            now_ts: float | None = None
            if coord.time_authority is not None:
                now_ts = coord.time_authority.corrected_epoch()
            derived = tracker.register_market(
                market_id=market_id,
                event_start_ts=event_start_ts,
                event_end_ts=event_end_ts,
                now_ts=now_ts,
            )
            if derived is not None and derived.price:
                from decimal import Decimal

                store.update_price_to_beat(
                    Decimal(derived.price),
                    status=derived.status,
                    observed_ts=derived.source_ts,
                    lag_ms=derived.boundary_lag_ms,
                )
            state.price_to_beat_tracker = tracker

        async def _reference_wrapper() -> None:
            local_stop = asyncio.Event()

            async def _relay() -> None:
                await stop.wait()
                local_stop.set()
                ref_stop.set()

            relay = asyncio.create_task(_relay())
            try:
                await run_reference_prices_ingest(
                    feeds=list(ref.feeds),
                    symbols=list(ref.symbols),
                    venue=ref.venue,
                    stop=local_stop,
                    on_event_emitted=_on_event,
                    price_to_beat_tracker=tracker,
                    reconnect_backoff_s=ref.reconnect_backoff_s,
                )
            finally:
                store.mark_chainlink_connected(connected=False)
                relay.cancel()
                try:
                    await relay
                except asyncio.CancelledError:
                    pass

        state.reference_stop = ref_stop
        state.reference_task = asyncio.create_task(_reference_wrapper())
        log.info("signal_feed_runtime: RTDS reference ingest started feeds=%s", ref.feeds)

    return state


async def stop_signal_feeds(state: SignalFeedRuntimeState, *, timeout_s: float = 15.0) -> None:
    """Cancel background signal feed tasks and wait for clean shutdown."""
    if state.binance_stop is not None:
        state.binance_stop.set()
    if state.reference_stop is not None:
        state.reference_stop.set()

    for task in (state.binance_task, state.reference_task):
        if task is None:
            continue
        try:
            await asyncio.wait_for(task, timeout=timeout_s)
        except TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            pass

    state.binance_task = None
    state.reference_task = None
    state.binance_stop = None
    state.reference_stop = None
    state.price_to_beat_tracker = None


def signal_feeds_requested(app: AppConfig) -> bool:
    """True when live signal feed runtime should start for the loaded app."""
    return bool(app.runtime.signal_feeds.enabled)
