"""Polymarket RTDS WebSocket client (M2B.3-A — read-only)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_RTDS_WS_URL = "wss://ws-live-data.polymarket.com"


def build_subscribe_message(
    *,
    topic: str,
    msg_type: str = "update",
    filters: str = "",
) -> dict[str, Any]:
    sub: dict[str, Any] = {"topic": topic, "type": msg_type}
    if filters:
        sub["filters"] = filters
    return {"action": "subscribe", "subscriptions": [sub]}


class PolymarketRtdsWsClient:
    """Thin async wrapper around Polymarket RTDS public streams."""

    def __init__(
        self,
        *,
        ws_url: str = DEFAULT_RTDS_WS_URL,
        subscriptions: list[dict[str, Any]],
    ) -> None:
        self.ws_url = ws_url.strip()
        self.subscriptions = subscriptions

    async def run(
        self,
        *,
        stop: asyncio.Event,
        on_message: Callable[[dict[str, Any]], Awaitable[None] | None],
        reconnect_backoff_s: float = 3.0,
        ping_interval_s: float = 5.0,
    ) -> None:
        try:
            import websockets
        except ImportError:
            log.error("websockets required for RTDS; pip install tyrex-pm[live]")
            await stop.wait()
            return

        while not stop.is_set():
            try:
                async with websockets.connect(self.ws_url, ping_interval=None, open_timeout=15) as ws:
                    await ws.send(
                        json.dumps({"action": "subscribe", "subscriptions": self.subscriptions})
                    )
                    log.info("reference_prices: connected url=%s subs=%s", self.ws_url, len(self.subscriptions))
                    ping_task = asyncio.create_task(self._ping_loop(ws, stop, ping_interval_s))
                    try:
                        while not stop.is_set():
                            raw = await ws.recv()
                            if isinstance(raw, bytes):
                                raw = raw.decode("utf-8", errors="replace")
                            if raw == "PONG":
                                continue
                            try:
                                msg = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            if isinstance(msg, dict):
                                maybe = on_message(msg)
                                if asyncio.iscoroutine(maybe):
                                    await maybe
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if stop.is_set():
                    break
                log.warning("reference_prices: ws error %r; reconnect in %ss", exc, reconnect_backoff_s)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=max(1.0, reconnect_backoff_s))
                except asyncio.TimeoutError:
                    continue

    async def _ping_loop(self, ws, stop: asyncio.Event, interval_s: float) -> None:
        while not stop.is_set():
            try:
                await ws.send("PING")
            except Exception:
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(1.0, interval_s))
                return
            except asyncio.TimeoutError:
                pass
