"""Polymarket RTDS WebSocket adapters (read-only, N2).

- Chainlink ``crypto_prices_chainlink`` / ``btc/usd`` → settlement reference ticks
- Optional Binance ``crypto_prices`` → comparison/fallback only
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from tyrex_pm.adapters.polymarket.rtds_normalize import (
    RTDS_URL,
    SYMBOL_BINANCE_BTC,
    SYMBOL_CHAINLINK_BTC,
    binance_subscribe_message,
    chainlink_subscribe_message,
    normalize_chainlink_tick,
    normalize_rtds_binance_tick,
)
from tyrex_pm.core.ids import CorrelationId, new_correlation_id
from tyrex_pm.core.ingress import ConnectionGeneration, IngressSequencer
from tyrex_pm.engine.dispatcher import EventDispatcher

logger = logging.getLogger(__name__)

HealthCallback = Callable[[str, dict], None]
BinanceMode = Literal["filtered", "unfiltered", "auto"]


class RtdsChainlinkAdapter:
    """Settlement-reference ticks from Polymarket RTDS Chainlink."""

    def __init__(
        self,
        *,
        url: str = RTDS_URL,
        symbol: str = SYMBOL_CHAINLINK_BTC,
        ping_interval_s: float = 5.0,
        heartbeat_timeout_s: float = 20.0,
        reconnect_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        correlation_id: CorrelationId | None = None,
        on_health: HealthCallback | None = None,
        clock_uncertainty_ms: int | None = None,
        sequencer: IngressSequencer | None = None,
        connect: Any | None = None,
        ssl: Any = None,
    ) -> None:
        self._url = url
        self._symbol = symbol.lower()
        self._ping_interval_s = ping_interval_s
        self._heartbeat_timeout_s = heartbeat_timeout_s
        self._reconnect_backoff_s = reconnect_backoff_s
        self._max_backoff_s = max_backoff_s
        self._correlation_id = correlation_id or new_correlation_id()
        self._on_health = on_health
        self._clock_uncertainty_ms = clock_uncertainty_ms
        self._sequencer = sequencer or IngressSequencer()
        self._conn_gen = ConnectionGeneration()
        self._connect = connect  # injectable for tests
        self._ssl = ssl  # optional; default system trust store
        self._stop = asyncio.Event()
        self._ws = None
        self._last_source_ts_ms: int | None = None
        self._last_msg_mono = 0.0
        self.ready = False

    @property
    def connection_generation(self) -> int:
        return self._conn_gen.value

    async def stop(self) -> None:
        self._stop.set()
        self.ready = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    def _health(self, status: str, **extra: object) -> None:
        if self._on_health:
            self._on_health(status, {"adapter": "rtds_chainlink", **extra})

    async def run(self, dispatcher: EventDispatcher) -> None:
        if self._connect is None:
            try:
                import websockets
            except ImportError as exc:
                raise RuntimeError("websockets package required for RTDS adapter") from exc
            connect = websockets.connect
        else:
            connect = self._connect

        backoff = self._reconnect_backoff_s
        while not self._stop.is_set():
            session_failed = False
            try:
                self.ready = False
                self._health("connecting", url=self._url)
                connect_kwargs: dict[str, Any] = {
                    "ping_interval": None,
                    "close_timeout": 5,
                }
                if self._ssl is not None:
                    connect_kwargs["ssl"] = self._ssl
                async with connect(self._url, **connect_kwargs) as ws:
                    self._ws = ws
                    gen = self._conn_gen.bump()
                    sub = chainlink_subscribe_message(filters="")
                    await ws.send(json.dumps(sub))
                    self._health(
                        "subscribed",
                        generation=gen,
                        topic="crypto_prices_chainlink",
                        symbol=self._symbol,
                    )
                    self._last_msg_mono = time.monotonic()
                    backoff = self._reconnect_backoff_s
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        while not self._stop.is_set():
                            try:
                                raw = await asyncio.wait_for(
                                    ws.recv(), timeout=self._heartbeat_timeout_s
                                )
                            except asyncio.TimeoutError:
                                session_failed = True
                                self.ready = False
                                self._health(
                                    "heartbeat_timeout",
                                    generation=gen,
                                    timeout_s=self._heartbeat_timeout_s,
                                )
                                break
                            self._last_msg_mono = time.monotonic()
                            await self._handle_raw(raw, dispatcher, generation=gen)
                            if not self.ready:
                                self.ready = True
                                self._health("ready", generation=gen)
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
                    if not self._stop.is_set() and not session_failed:
                        session_failed = True
                        self._health("reconnecting", error="connection_closed", generation=gen)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                session_failed = True
                self.ready = False
                self._health(
                    "reconnecting",
                    error=type(exc).__name__,
                    message=str(exc),
                )
            finally:
                self._ws = None
                self.ready = False
            if self._stop.is_set():
                break
            if session_failed:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff_s)
        self._health("disconnected")

    async def _ping_loop(self, ws: Any) -> None:
        while not self._stop.is_set():
            try:
                await ws.send("PING")
            except Exception:
                return
            await asyncio.sleep(self._ping_interval_s)

    async def _handle_raw(
        self, raw: str | bytes, dispatcher: EventDispatcher, *, generation: int
    ) -> None:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        if not text or text in ("PONG", "PING"):
            return
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("non-json RTDS message ignored")
            return
        if not isinstance(msg, dict):
            return
        wall = datetime.now(timezone.utc)
        mono = time.perf_counter_ns()
        event = normalize_chainlink_tick(
            msg,
            ts_received=wall,
            receive_monotonic_ns=mono,
            ingress_sequence=self._sequencer.next(),
            connection_generation=generation,
            clock_uncertainty_ms=self._clock_uncertainty_ms,
            correlation_id=self._correlation_id,
            expected_symbol=self._symbol,
            last_source_ts_ms=self._last_source_ts_ms,
        )
        if event is None:
            return
        self._last_source_ts_ms = int(event.ts_event.timestamp() * 1000)
        dispatcher.publish(event)


class RtdsBinanceComparisonAdapter:
    """Optional RTDS Binance comparison/fallback — never primary trading S."""

    def __init__(
        self,
        *,
        url: str = RTDS_URL,
        symbol: str = SYMBOL_BINANCE_BTC,
        mode: BinanceMode = "auto",
        filtered_idle_s: float = 8.0,
        ping_interval_s: float = 5.0,
        heartbeat_timeout_s: float = 20.0,
        reconnect_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        correlation_id: CorrelationId | None = None,
        on_health: HealthCallback | None = None,
        clock_uncertainty_ms: int | None = None,
        sequencer: IngressSequencer | None = None,
        connect: Any | None = None,
        ssl: Any = None,
    ) -> None:
        self._url = url
        self._symbol = symbol.lower()
        self._mode_pref = mode
        self._filtered_idle_s = filtered_idle_s
        self._ping_interval_s = ping_interval_s
        self._heartbeat_timeout_s = heartbeat_timeout_s
        self._reconnect_backoff_s = reconnect_backoff_s
        self._max_backoff_s = max_backoff_s
        self._correlation_id = correlation_id or new_correlation_id()
        self._on_health = on_health
        self._clock_uncertainty_ms = clock_uncertainty_ms
        self._sequencer = sequencer or IngressSequencer()
        self._conn_gen = ConnectionGeneration()
        self._connect = connect
        self._ssl = ssl
        self._stop = asyncio.Event()
        self._ws = None
        self._last_source_ts_ms: int | None = None
        self.active_mode: str | None = None
        self.ready = False

    @property
    def connection_generation(self) -> int:
        return self._conn_gen.value

    async def stop(self) -> None:
        self._stop.set()
        self.ready = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    def _health(self, status: str, **extra: object) -> None:
        if self._on_health:
            self._on_health(
                status,
                {
                    "adapter": "rtds_binance_comparison",
                    "role": "comparison_fallback",
                    "subscription_mode": self.active_mode,
                    **extra,
                },
            )

    async def run(self, dispatcher: EventDispatcher) -> None:
        if self._connect is None:
            try:
                import websockets
            except ImportError as exc:
                raise RuntimeError("websockets package required for RTDS adapter") from exc
            connect = websockets.connect
        else:
            connect = self._connect

        backoff = self._reconnect_backoff_s
        while not self._stop.is_set():
            try:
                self.ready = False
                # Resolve mode for this connection generation
                if self._mode_pref == "auto":
                    modes_to_try: list[str] = ["filtered", "unfiltered"]
                else:
                    modes_to_try = [self._mode_pref]
                connected = False
                for mode in modes_to_try:
                    if self._stop.is_set():
                        break
                    ok = await self._run_session(
                        connect, dispatcher, mode=mode, allow_failover=(mode == "filtered")
                    )
                    if ok:
                        connected = True
                        break
                    if mode == "filtered" and "unfiltered" in modes_to_try:
                        self._health(
                            "subscription_mode_fallback",
                            from_mode="filtered",
                            to_mode="unfiltered",
                            reason="no_btcusdt_messages",
                        )
                if not connected and not self._stop.is_set():
                    self._health("reconnecting", error="session_failed")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self._max_backoff_s)
                else:
                    backoff = self._reconnect_backoff_s
            except asyncio.CancelledError:
                raise
        self._health("disconnected")

    async def _run_session(
        self,
        connect: Any,
        dispatcher: EventDispatcher,
        *,
        mode: str,
        allow_failover: bool,
    ) -> bool:
        """Return True if session ran until stop; False if should try next mode/reconnect."""
        self.active_mode = mode
        try:
            self._health("connecting", url=self._url, mode=mode)
            connect_kwargs: dict[str, Any] = {
                "ping_interval": None,
                "close_timeout": 5,
            }
            if self._ssl is not None:
                connect_kwargs["ssl"] = self._ssl
            async with connect(self._url, **connect_kwargs) as ws:
                self._ws = ws
                gen = self._conn_gen.bump()
                await ws.send(json.dumps(binance_subscribe_message(mode=mode)))
                self._health("subscribed", generation=gen, mode=mode)
                ping_task = asyncio.create_task(self._ping_loop(ws))
                got_btc = False
                idle_deadline = time.monotonic() + self._filtered_idle_s
                try:
                    while not self._stop.is_set():
                        timeout = self._heartbeat_timeout_s
                        if allow_failover and not got_btc:
                            timeout = min(timeout, max(0.5, idle_deadline - time.monotonic()))
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        except asyncio.TimeoutError:
                            if allow_failover and not got_btc:
                                return False  # try unfiltered
                            self.ready = False
                            self._health("heartbeat_timeout", generation=gen)
                            return False
                        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
                        if not text or text in ("PONG", "PING"):
                            continue
                        try:
                            msg = json.loads(text)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(msg, dict):
                            continue
                        wall = datetime.now(timezone.utc)
                        mono = time.perf_counter_ns()
                        event = normalize_rtds_binance_tick(
                            msg,
                            ts_received=wall,
                            receive_monotonic_ns=mono,
                            ingress_sequence=self._sequencer.next(),
                            connection_generation=gen,
                            clock_uncertainty_ms=self._clock_uncertainty_ms,
                            subscription_mode=mode,
                            correlation_id=self._correlation_id,
                            expected_symbol=self._symbol,
                            last_source_ts_ms=self._last_source_ts_ms,
                        )
                        if event is None:
                            continue
                        got_btc = True
                        self._last_source_ts_ms = int(event.ts_event.timestamp() * 1000)
                        dispatcher.publish(event)
                        if not self.ready:
                            self.ready = True
                            self._health("ready", generation=gen, mode=mode)
                finally:
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ready = False
            self._health("reconnecting", error=type(exc).__name__, message=str(exc), mode=mode)
            return False
        finally:
            self._ws = None
            self.ready = False

    async def _ping_loop(self, ws: Any) -> None:
        while not self._stop.is_set():
            try:
                await ws.send("PING")
            except Exception:
                return
            await asyncio.sleep(self._ping_interval_s)
