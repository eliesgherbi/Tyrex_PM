"""Live Binance public trade WebSocket adapter (read-only)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable

from tyrex_pm.adapters.binance.normalize import normalize_trade_message
from tyrex_pm.core.ids import CorrelationId, new_correlation_id
from tyrex_pm.engine.dispatcher import EventDispatcher

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
    ) -> None:
        self._symbol = symbol.upper()
        self._url = binance_trade_ws_url(self._symbol)
        self._correlation_id = correlation_id or new_correlation_id()
        self._on_health = on_health
        self._reset_indicator = reset_indicator
        self._stop = asyncio.Event()
        self._ws = None
        self._ever_connected = False

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()

    def _health(self, status: str, **extra: object) -> None:
        if self._on_health:
            self._on_health(status, dict(extra))

    async def run(self, dispatcher: EventDispatcher) -> None:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("websockets package required for live Binance adapter") from exc

        backoff = 1.0
        while not self._stop.is_set():
            session_failed = False
            try:
                self._health("connecting", url=self._url)
                async with websockets.connect(self._url, ping_interval=20, ping_timeout=20) as ws:
                    self._ws = ws
                    if self._ever_connected and self._reset_indicator is not None:
                        self._reset_indicator()
                    self._ever_connected = True
                    self._health("connected", symbol=self._symbol)
                    backoff = 1.0
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        await self._handle_raw(raw, dispatcher)
                    if not self._stop.is_set():
                        session_failed = True
                        self._health("reconnecting", error="connection_closed", message="websocket ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                session_failed = True
                self._health("reconnecting", error=type(exc).__name__, message=str(exc))
            finally:
                self._ws = None
            if self._stop.is_set():
                break
            if session_failed:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
        self._health("disconnected")

    async def _handle_raw(self, raw: str | bytes, dispatcher: EventDispatcher) -> None:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("non-json binance ws message ignored")
            return
        ts_received = datetime.now(timezone.utc)
        event = normalize_trade_message(
            payload,
            ts_received=ts_received,
            correlation_id=self._correlation_id,
            symbol=self._symbol,
        )
        dispatcher.publish(event)
