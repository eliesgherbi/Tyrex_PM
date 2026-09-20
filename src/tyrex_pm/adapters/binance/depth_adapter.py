"""Live Binance spot L2 depth WebSocket (read-only). Mid is derived from this book."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from tyrex_pm.core.ids import InstrumentId
from tyrex_pm.indicators.depth import DepthLevel, DepthSnapshot, DepthStore

logger = logging.getLogger(__name__)


def binance_depth_ws_url(symbol: str) -> str:
    return f"wss://stream.binance.com:9443/ws/{symbol.lower()}@depth20@100ms"


def depth_snapshot_from_binance(
    payload: dict[str, Any],
    *,
    symbol: str,
    ts_event: datetime,
) -> DepthSnapshot:
    data = payload.get("data", payload)
    bids_raw = data.get("bids") or data.get("b") or ()
    asks_raw = data.get("asks") or data.get("a") or ()
    bids = tuple(
        DepthLevel(price=Decimal(str(price)), quantity=Decimal(str(qty)))
        for price, qty in bids_raw
    )
    asks = tuple(
        DepthLevel(price=Decimal(str(price)), quantity=Decimal(str(qty)))
        for price, qty in asks_raw
    )
    return DepthSnapshot(
        instrument_id=InstrumentId(symbol.upper()),
        ts_event=ts_event,
        bids=bids,
        asks=asks,
    )


class BinanceDepthWsAdapter:
    def __init__(
        self,
        *,
        symbol: str,
        store: DepthStore,
        on_update: Callable[[DepthSnapshot], None] | None = None,
        on_health: Callable[[str, dict], None] | None = None,
        reconnect_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
    ) -> None:
        self._symbol = symbol.upper()
        self._url = binance_depth_ws_url(self._symbol)
        self._store = store
        self._on_update = on_update
        self._on_health = on_health
        self._reconnect_backoff_s = reconnect_backoff_s
        self._max_backoff_s = max_backoff_s
        self._stop = asyncio.Event()
        self._ws = None
        self.ready = False

    async def stop(self) -> None:
        self._stop.set()
        self.ready = False
        if self._ws is not None:
            await self._ws.close()

    def _health(self, status: str, **extra: object) -> None:
        if self._on_health:
            self._on_health(status, {"adapter": "binance_spot_l2", **extra})

    async def run(self) -> None:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("websockets package required for live Binance L2 adapter") from exc

        backoff = self._reconnect_backoff_s
        while not self._stop.is_set():
            session_failed = False
            try:
                self.ready = False
                self._health("connecting", url=self._url)
                async with websockets.connect(self._url, ping_interval=20, ping_timeout=20) as ws:
                    self._ws = ws
                    self._health("connected", symbol=self._symbol)
                    backoff = self._reconnect_backoff_s
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        self._handle_raw(raw)
                        if not self.ready:
                            self.ready = True
                            self._health("ready")
                    if not self._stop.is_set():
                        session_failed = True
                        self.ready = False
                        self._health("reconnecting", error="connection_closed")
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

    def _handle_raw(self, raw: str | bytes) -> None:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("non-json binance depth message ignored")
            return
        if not isinstance(payload, dict):
            return
        snapshot = depth_snapshot_from_binance(
            payload,
            symbol=self._symbol,
            ts_event=datetime.now(timezone.utc),
        )
        applied = self._store.apply(snapshot)
        if self._on_update is not None:
            self._on_update(applied)
