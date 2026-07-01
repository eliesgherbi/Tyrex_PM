"""Polymarket market WebSocket ingestion (Phase 2 M1 shadow / M8 WS-primary).

Shadow mode (M1): writes only to ``MarketStateStoreShadow``; REST stays authoritative.
Primary mode (M8): writes to ``coord.market_state`` with ``source_quality=WS_PRIMARY``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from typing import Any

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.ingestion.market_stream import apply_market_message
from tyrex_pm.market_data.models import BookSource, RawMarketEvent, SourceQuality
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_MARKET_WS_CONNECTED,
    FACT_TYPE_MARKET_WS_DISCONNECTED,
    FACT_TYPE_OUT_OF_ORDER_EVENT,
    FACT_TYPE_WS_PRIMARY_CUTOVER,
    FACT_TYPE_WS_SEQUENCE_GAP_DETECTED,
    FACT_TYPE_WS_VS_REST_BOOK_COMPARE,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.state.market_store import MarketStateStore

log = logging.getLogger(__name__)

DEFAULT_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def market_ws_shadow_enabled(*, env: os._Environ[str] | None = None, config_flag: bool = False) -> bool:
    env_map = env or os.environ
    raw = str(env_map.get("TYREX_MARKET_WS_SHADOW", "")).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return config_flag


def market_ws_primary_enabled(*, env: os._Environ[str] | None = None, config_flag: bool = False) -> bool:
    env_map = env or os.environ
    raw = str(env_map.get("TYREX_MARKET_WS_PRIMARY", "")).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return config_flag


def _parse_messages(raw: str | bytes) -> list[dict[str, Any]]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if raw == "PONG":
        return []
    payload = json.loads(raw)
    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]
    return [payload] if isinstance(payload, dict) else []


def _emit(
    sink: JsonlSink | None,
    run_id: RunId | str | None,
    fact_type: str,
    payload: dict[str, Any],
) -> None:
    if sink is None or run_id is None:
        return
    sink.write(make_fact(fact_type, str(run_id), payload))


def _compare_books(
    *,
    token_id: TokenId,
    authoritative: MarketStateStore,
    shadow: MarketStateStore,
    sink: JsonlSink | None,
    run_id: RunId | str | None,
) -> None:
    auth = authoritative.capture(token_id)
    sh = shadow.capture(token_id)
    if auth is None and sh is None:
        return
    payload: dict[str, Any] = {
        "token_id": str(token_id),
        "rest_has_book": auth is not None,
        "ws_has_book": sh is not None,
    }
    if auth is not None:
        payload.update(
            {
                "rest_age_ms": auth.book_age_ms,
                "rest_best_bid": str(auth.best_bid) if auth.best_bid is not None else None,
                "rest_best_ask": str(auth.best_ask) if auth.best_ask is not None else None,
                "rest_source": auth.source,
            }
        )
    if sh is not None:
        payload.update(
            {
                "ws_age_ms": sh.book_age_ms,
                "ws_best_bid": str(sh.best_bid) if sh.best_bid is not None else None,
                "ws_best_ask": str(sh.best_ask) if sh.best_ask is not None else None,
                "ws_source": sh.source,
            }
        )
    if auth is not None and sh is not None:
        if auth.best_bid is not None and sh.best_bid is not None:
            payload["bid_delta"] = str(sh.best_bid - auth.best_bid)
        if auth.best_ask is not None and sh.best_ask is not None:
            payload["ask_delta"] = str(sh.best_ask - auth.best_ask)
        payload["ws_fresher_than_rest"] = sh.book_age_ms < auth.book_age_ms
    _emit(sink, run_id, FACT_TYPE_WS_VS_REST_BOOK_COMPARE, payload)


def _handle_sequence(
    store: MarketStateStore,
    token_id: TokenId,
    *,
    book_hash: str | None,
    sink: JsonlSink | None,
    run_id: RunId | str | None,
    primary_mode: bool = False,
    on_sequence_gap: Callable[[TokenId], Any] | None = None,
) -> bool:
    """Return True if event should be applied; False if duplicate/out-of-order."""
    if not book_hash:
        return True
    last = store.last_book_hash(token_id)
    if last is not None and book_hash == last:
        return False
    if last is not None and book_hash < last:
        _emit(
            sink,
            run_id,
            FACT_TYPE_OUT_OF_ORDER_EVENT,
            {
                "token_id": str(token_id),
                "book_hash": book_hash,
                "last_book_hash": last,
            },
        )
        if primary_mode:
            store.set_reconnect_gap(token_id, True)
            _emit(
                sink,
                run_id,
                FACT_TYPE_WS_SEQUENCE_GAP_DETECTED,
                {"token_id": str(token_id), "reason": "out_of_order"},
            )
            if on_sequence_gap is not None:
                on_sequence_gap(token_id)
        return False
    return True


async def _ping_loop(ws, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await ws.send("PING")
        except Exception:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=10.0)
            return
        except asyncio.TimeoutError:
            pass


async def run_market_ws_ingest(
    coord,
    token_ids: list[str],
    *,
    stop: asyncio.Event,
    shadow_store: MarketStateStore | None,
    authoritative_store: MarketStateStore,
    primary_mode: bool = False,
    on_raw_event: Callable[[RawMarketEvent], None] | None = None,
    on_ws_connected: Callable[[], Any] | None = None,
    on_ws_disconnected: Callable[[], Any] | None = None,
    on_book_applied: Callable[[TokenId], Any] | None = None,
    sink: JsonlSink | None = None,
    run_id: RunId | str | None = None,
    url: str | None = None,
    reconnect_backoff_s: float = 3.0,
    compare_interval_s: float = 5.0,
    on_gap_recovery: Callable[[], Any] | None = None,
) -> None:
    """Market WS ingest — shadow (M1) or WS-primary authoritative (M8)."""
    ws_url = (url or os.environ.get("TYREX_MARKET_WS_URL") or DEFAULT_MARKET_WS_URL).strip()
    if not token_ids:
        log.warning("market_ws_ingest: no token_ids; exiting")
        return

    if primary_mode:
        _emit(
            sink,
            run_id,
            FACT_TYPE_WS_PRIMARY_CUTOVER,
            {"mode": "ws_primary", "authoritative": "coord.market_state"},
        )

    write_store = authoritative_store if primary_mode else shadow_store
    if write_store is None:
        log.error("market_ws_ingest: no target store")
        return

    try:
        import websockets
    except ImportError:
        log.error("websockets package required for market WS; pip install tyrex-pm[live]")
        await stop.wait()
        return

    connection_id = str(uuid.uuid4())
    local_counter = 0
    last_compare = 0.0
    had_sequence = False

    async def _invoke(cb: Callable[[], Any] | None) -> None:
        if cb is None:
            return
        result = cb()
        if asyncio.iscoroutine(result):
            await result

    def _on_gap(token_id: TokenId) -> None:
        for tid_str in token_ids:
            authoritative_store.set_reconnect_gap(TokenId(str(tid_str)), True)

    while not stop.is_set():
        try:
            async with websockets.connect(ws_url, ping_interval=None, open_timeout=15) as ws:
                sub = {
                    "assets_ids": [str(t) for t in token_ids],
                    "type": "market",
                    "custom_feature_enabled": True,
                }
                await ws.send(json.dumps(sub))
                _emit(
                    sink,
                    run_id,
                    FACT_TYPE_MARKET_WS_CONNECTED,
                    {
                        "url": ws_url,
                        "connection_id": connection_id,
                        "token_count": len(token_ids),
                        "primary_mode": primary_mode,
                    },
                )
                await _invoke(on_ws_connected)
                ping_task = asyncio.create_task(_ping_loop(ws, stop))
                try:
                    while not stop.is_set():
                        raw = await ws.recv()
                        local_counter += 1
                        received_ts = utc_now()
                        for msg in _parse_messages(raw):
                            event = RawMarketEvent(
                                raw_event_id=str(uuid.uuid4()),
                                channel="market",
                                payload=msg,
                                received_ts=received_ts,
                                received_monotonic_ns=time.monotonic_ns(),
                                connection_id=connection_id,
                                local_event_counter=local_counter,
                            )
                            if on_raw_event is not None:
                                on_raw_event(event)
                            asset = msg.get("asset_id")
                            if asset is None and msg.get("price_changes"):
                                pcs = msg.get("price_changes") or []
                                if pcs and isinstance(pcs[0], dict):
                                    asset = pcs[0].get("asset_id")
                            tid = TokenId(str(asset)) if asset else None
                            book_hash = msg.get("hash")
                            if book_hash:
                                had_sequence = True
                            sequence_gap = False

                            def _on_sequence_gap(token_id: TokenId) -> None:
                                nonlocal sequence_gap
                                sequence_gap = True
                                _on_gap(token_id)

                            if tid is not None and not _handle_sequence(
                                write_store,
                                tid,
                                book_hash=book_hash,
                                sink=sink,
                                run_id=run_id,
                                primary_mode=primary_mode,
                                on_sequence_gap=_on_sequence_gap if primary_mode else None,
                            ):
                                if (
                                    primary_mode
                                    and on_gap_recovery is not None
                                    and sequence_gap
                                ):
                                    await _invoke(on_gap_recovery)
                                continue
                            applied = apply_market_message(
                                write_store, msg, source=BookSource.WEBSOCKET
                            )
                            if not applied:
                                continue
                            if primary_mode and tid is not None:
                                cap = write_store.capture(tid)
                                if cap is not None and cap.source_quality == SourceQuality.WS_PRIMARY:
                                    write_store.set_reconnect_gap(tid, False)
                            if on_book_applied is not None and tid is not None:
                                result = on_book_applied(tid)
                                if asyncio.iscoroutine(result):
                                    await result
                            if not primary_mode and shadow_store is not None:
                                now_mono = asyncio.get_event_loop().time()
                                if now_mono - last_compare >= compare_interval_s:
                                    last_compare = now_mono
                                    for tid_str in token_ids:
                                        _compare_books(
                                            token_id=TokenId(str(tid_str)),
                                            authoritative=authoritative_store,
                                            shadow=shadow_store,
                                            sink=sink,
                                            run_id=run_id,
                                        )
                finally:
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("market websocket session ended; reconnecting")
            _emit(
                sink,
                run_id,
                FACT_TYPE_MARKET_WS_DISCONNECTED,
                {"url": ws_url, "connection_id": connection_id, "primary_mode": primary_mode},
            )
            await _invoke(on_ws_disconnected)
            gap_store = authoritative_store if primary_mode else write_store
            for tid_str in token_ids:
                tid = TokenId(str(tid_str))
                gap_store.set_reconnect_gap(tid, True)
                _emit(
                    sink,
                    run_id,
                    FACT_TYPE_WS_SEQUENCE_GAP_DETECTED,
                    {"token_id": str(tid), "reason": "ws_disconnect"},
                )
            if on_gap_recovery is not None:
                try:
                    await _invoke(on_gap_recovery)
                except Exception:
                    log.exception("REST recovery after WS gap failed")
            elif primary_mode and not had_sequence and on_gap_recovery is None:
                pass

        try:
            await asyncio.wait_for(stop.wait(), timeout=reconnect_backoff_s)
            return
        except asyncio.TimeoutError:
            connection_id = str(uuid.uuid4())
            local_counter = 0
            continue
