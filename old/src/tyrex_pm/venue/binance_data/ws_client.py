"""Binance public WebSocket client (M2B.2 — data-only)."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_BINANCE_WS_BASE = "wss://stream.binance.com:9443"


def build_combined_stream_url(
    symbol: str,
    streams: list[str] | tuple[str, ...],
    *,
    ws_base: str = DEFAULT_BINANCE_WS_BASE,
) -> str:
    """Build a Binance combined-stream URL for *symbol* and stream names."""
    sym = str(symbol).strip().lower()
    parts = [f"{sym}@{str(stream).strip()}" for stream in streams if str(stream).strip()]
    if not parts:
        raise ValueError("at least one stream is required")
    return f"{ws_base.rstrip('/')}/stream?streams={'/'.join(parts)}"


def parse_binance_ws_message(raw: str | bytes) -> tuple[str | None, dict[str, Any] | None]:
    """Return (stream_name, data) from a combined-stream envelope."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    stream = payload.get("stream")
    data = payload.get("data")
    if isinstance(data, dict):
        return str(stream) if stream is not None else None, data
    if "e" in payload:
        return str(payload.get("e")), payload
    return None, payload if isinstance(payload, dict) else None


class BinanceDataWsClient:
    """Thin async wrapper around Binance public combined streams."""

    def __init__(
        self,
        *,
        symbol: str,
        streams: list[str] | tuple[str, ...],
        ws_base: str = DEFAULT_BINANCE_WS_BASE,
    ) -> None:
        self.symbol = str(symbol).strip().upper()
        self.streams = tuple(str(s).strip() for s in streams if str(s).strip())
        self.ws_url = build_combined_stream_url(self.symbol, self.streams, ws_base=ws_base)

    async def run(
        self,
        *,
        stop,
        on_message: Callable[[str | None, dict[str, Any]], Awaitable[None] | None],
        reconnect_backoff_s: float = 3.0,
    ) -> None:
        try:
            import websockets
        except ImportError:
            log.error("websockets package required for external BTC feed; pip install tyrex-pm[live]")
            await stop.wait()
            return

        import asyncio

        while not stop.is_set():
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, open_timeout=15) as ws:
                    log.info("external_btc: connected url=%s", self.ws_url)
                    while not stop.is_set():
                        raw = await ws.recv()
                        stream_name, data = parse_binance_ws_message(raw)
                        if data is None:
                            continue
                        maybe = on_message(stream_name, data)
                        if asyncio.iscoroutine(maybe):
                            await maybe
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if stop.is_set():
                    break
                log.warning("external_btc: ws error %r; reconnect in %ss", exc, reconnect_backoff_s)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=max(1.0, reconnect_backoff_s))
                except asyncio.TimeoutError:
                    continue
