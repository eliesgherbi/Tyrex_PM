"""Live Binance public trade WebSocket adapter (read-only).

Primary fast trading reference (S) for N2+. Never settlement truth.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from tyrex_pm.adapters.binance.normalize import normalize_trade_message
from tyrex_pm.core.ids import CorrelationId, new_correlation_id
from tyrex_pm.core.ingress import ConnectionGeneration, IngressSequencer
from tyrex_pm.engine.dispatcher import DispatchError, EventDispatcher

logger = logging.getLogger(__name__)


def binance_trade_ws_url(symbol: str) -> str:
    return f"wss://stream.binance.com:9443/ws/{symbol.lower()}@trade"


class BinanceTradeWsAdapter:
    def __init__(
        self,
        *,
        symbol: str,
        correlation_id: CorrelationId | None = None,
        on_health: Callable[[str, dict], None] | None = None,
        reset_indicator: Callable[[], None] | None = None,
        clock_uncertainty_ms: int | None = None,
        sequencer: IngressSequencer | None = None,
        reconnect_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        time_authority: Any | None = None,
    ) -> None:
        self._symbol = symbol.upper()
        self._url = binance_trade_ws_url(self._symbol)
        self._correlation_id = correlation_id or new_correlation_id()
        self._on_health = on_health
        self._reset_indicator = reset_indicator
        self._clock_uncertainty_ms = clock_uncertainty_ms
        self._sequencer = sequencer or IngressSequencer()
        self._conn_gen = ConnectionGeneration()
        self._reconnect_backoff_s = reconnect_backoff_s
        self._max_backoff_s = max_backoff_s
        self._time_authority = time_authority
        self._stop = asyncio.Event()
        self._ws = None
        self._ever_connected = False
        self._last_trade_id: int | None = None
        self.ready = False

    @property
    def connection_generation(self) -> int:
        return self._conn_gen.value

    async def stop(self) -> None:
        self._stop.set()
        self.ready = False
        if self._ws is not None:
            await self._ws.close()

    def _health(self, status: str, **extra: object) -> None:
        if self._on_health:
            self._on_health(
                status,
                {"adapter": "binance_spot", "role": "trading_reference", **extra},
            )

    async def run(self, dispatcher: EventDispatcher) -> None:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("websockets package required for live Binance adapter") from exc

        backoff = self._reconnect_backoff_s
        while not self._stop.is_set():
            session_failed = False
            try:
                self.ready = False
                self._health("connecting", url=self._url)
                async with websockets.connect(self._url, ping_interval=20, ping_timeout=20) as ws:
                    self._ws = ws
                    gen = self._conn_gen.bump()
                    if self._ever_connected and self._reset_indicator is not None:
                        self._reset_indicator()
                    self._ever_connected = True
                    self._health("connected", symbol=self._symbol, generation=gen)
                    backoff = self._reconnect_backoff_s
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        await self._handle_raw(raw, dispatcher, generation=gen)
                        if not self.ready:
                            self.ready = True
                            self._health("ready", generation=gen)
                    if not self._stop.is_set():
                        session_failed = True
                        self.ready = False
                        self._health(
                            "reconnecting",
                            error="connection_closed",
                            message="websocket ended",
                            generation=gen,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                session_failed = True
                self.ready = False
                self._health("reconnecting", error=type(exc).__name__, message=str(exc))
            finally:
                self._ws = None
                self.ready = False
            if self._stop.is_set():
                break
            if session_failed:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff_s)
        self._health("disconnected")

    async def _handle_raw(
        self, raw: str | bytes, dispatcher: EventDispatcher, *, generation: int
    ) -> None:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("non-json binance ws message ignored")
            return
        ts_received = datetime.now(timezone.utc)
        time_view = None
        if self._time_authority is not None:
            time_view = self._time_authority.view()
        event = normalize_trade_message(
            payload,
            ts_received=ts_received,
            correlation_id=self._correlation_id,
            symbol=self._symbol,
            receive_monotonic_ns=time.perf_counter_ns(),
            clock_uncertainty_ms=self._clock_uncertainty_ms,
            ingress_sequence=self._sequencer.next(),
            connection_generation=generation,
            last_trade_id=self._last_trade_id,
            time_view=time_view,
        )
        trade_id = payload.get("data", payload).get("t") if isinstance(payload, dict) else None
        if trade_id is not None:
            self._last_trade_id = int(trade_id)
        try:
            dispatcher.publish(event)
        except DispatchError as exc:
            # A downstream consumer failure is not a WebSocket failure. Keep
            # the healthy socket alive and expose separate handler health.
            self._health(
                "handler_error",
                error=type(exc.cause).__name__,
                message="event_dispatch_handler_failed",
                event_id=exc.event_id.value,
                generation=generation,
            )
            logger.exception("Binance event handler failed; socket remains connected")
