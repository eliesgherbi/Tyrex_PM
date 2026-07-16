"""Live Polymarket CLOB market WebSocket adapter (read-only)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable

from tyrex_pm.adapters.polymarket.normalize import normalize_market_ws_message
from tyrex_pm.core.ids import CorrelationId, MarketId, new_correlation_id
from tyrex_pm.engine.dispatcher import EventDispatcher

logger = logging.getLogger(__name__)

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class PolymarketMarketWsAdapter:
    def __init__(
        self,
        *,
        asset_ids: list[str],
        market_id: MarketId | None = None,
        url: str = MARKET_WS_URL,
        correlation_id: CorrelationId | None = None,
        on_health: Callable[[str, dict], None] | None = None,
    ) -> None:
        if not asset_ids:
            raise ValueError("asset_ids required")
        self._asset_ids = asset_ids
        self._market_id = market_id
        self._url = url
        self._correlation_id = correlation_id or new_correlation_id()
        self._on_health = on_health
        self._stop = asyncio.Event()
        self._ws = None

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
            raise RuntimeError("websockets package required for live Polymarket adapter") from exc

        backoff = 1.0
        while not self._stop.is_set():
            session_failed = False
            try:
                self._health("connecting", url=self._url)
                async with websockets.connect(self._url, ping_interval=20, ping_timeout=20) as ws:
                    self._ws = ws
                    sub = {"assets_ids": self._asset_ids, "type": "market"}
                    await ws.send(json.dumps(sub))
                    self._health("connected", assets=self._asset_ids)
                    backoff = 1.0
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        await self._handle_raw(raw, dispatcher)
                    # Socket closed without stop request → treat as reconnect path.
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
        if not text or text == "PONG":
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("non-json polymarket ws message ignored")
            return
        messages = payload if isinstance(payload, list) else [payload]
        ts_received = datetime.now(timezone.utc)
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            event = normalize_market_ws_message(
                msg,
                ts_received=ts_received,
                correlation_id=self._correlation_id,
                market_id=self._market_id,
            )
            if event is not None:
                dispatcher.publish(event)
