"""Live Polymarket CLOB market WebSocket adapter (read-only)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from tyrex_pm.adapters.polymarket.normalize import normalize_market_ws_message
from tyrex_pm.core.ids import CorrelationId, MarketId, new_correlation_id
from tyrex_pm.core.ingress import ConnectionGeneration, FeedRole, IngressSequencer
from tyrex_pm.domain.polymarket.discovery_binding import (
    DiscoveredMarketBinding,
    DiscoverySessionRole,
)
from tyrex_pm.engine.dispatcher import EventDispatcher

logger = logging.getLogger(__name__)

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class PolymarketMarketWsAdapter:
    """Subscribe to CLOB market books by validated token IDs only."""

    def __init__(
        self,
        *,
        asset_ids: list[str],
        market_id: MarketId | None = None,
        window_slug: str | None = None,
        session_role: DiscoverySessionRole = DiscoverySessionRole.ACTIVE,
        url: str = MARKET_WS_URL,
        correlation_id: CorrelationId | None = None,
        on_health: Callable[[str, dict], None] | None = None,
        publish_events: bool = True,
        reconnect_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        ssl: Any = None,
    ) -> None:
        if not asset_ids:
            raise ValueError("asset_ids required")
        self._asset_ids = [str(a) for a in asset_ids]
        self._allowed = set(self._asset_ids)
        self._market_id = market_id
        self._window_slug = window_slug
        self._session_role = session_role
        self._url = url
        self._correlation_id = correlation_id or new_correlation_id()
        self._on_health = on_health
        self._publish_events = publish_events
        self._reconnect_backoff_s = reconnect_backoff_s
        self._max_backoff_s = max_backoff_s
        self._ssl = ssl
        self._stop = asyncio.Event()
        self._ws = None
        self._conn_gen = ConnectionGeneration()
        self._sequencer = IngressSequencer()
        self.ready = False
        self.rejected_wrong_token = 0

    @classmethod
    def from_binding(
        cls,
        binding: DiscoveredMarketBinding,
        *,
        publish_events: bool | None = None,
        url: str = MARKET_WS_URL,
        correlation_id: CorrelationId | None = None,
        on_health: Callable[[str, dict], None] | None = None,
        ssl: Any = None,
    ) -> PolymarketMarketWsAdapter:
        """Build adapter from a validated discovery binding.

        Prepared-next bindings default to ``publish_events=False`` so books are
        not treated as active until N4 promotes the session.
        """
        if publish_events is None:
            publish_events = binding.session_role is DiscoverySessionRole.ACTIVE
        return cls(
            asset_ids=binding.clob_asset_ids,
            market_id=binding.market.market_id,
            window_slug=binding.window_slug,
            session_role=binding.session_role,
            publish_events=publish_events,
            url=url,
            correlation_id=correlation_id,
            on_health=on_health,
            ssl=ssl,
        )

    @property
    def connection_generation(self) -> int:
        return self._conn_gen.value

    @property
    def session_role(self) -> DiscoverySessionRole:
        return self._session_role

    async def stop(self) -> None:
        self._stop.set()
        self.ready = False
        if self._ws is not None:
            await self._ws.close()

    def _health(self, status: str, **extra: object) -> None:
        if self._on_health:
            self._on_health(
                status,
                {
                    "adapter": "polymarket_clob",
                    "role": FeedRole.MARKET_BOOK.value,
                    "session_role": self._session_role.value,
                    "window_slug": self._window_slug,
                    "publish_events": self._publish_events,
                    **extra,
                },
            )

    async def run(self, dispatcher: EventDispatcher) -> None:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("websockets package required for live Polymarket adapter") from exc

        backoff = self._reconnect_backoff_s
        while not self._stop.is_set():
            session_failed = False
            try:
                self.ready = False
                self._health("connecting", url=self._url)
                connect_kwargs: dict[str, Any] = {
                    "ping_interval": 20,
                    "ping_timeout": 20,
                }
                if self._ssl is not None:
                    connect_kwargs["ssl"] = self._ssl
                async with websockets.connect(self._url, **connect_kwargs) as ws:
                    self._ws = ws
                    gen = self._conn_gen.bump()
                    sub = {"assets_ids": self._asset_ids, "type": "market"}
                    await ws.send(json.dumps(sub))
                    self._health(
                        "subscribed",
                        assets=self._asset_ids,
                        generation=gen,
                    )
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

    def _asset_id_from_msg(self, msg: dict) -> str | None:
        for key in ("asset_id", "assetId", "asset"):
            if msg.get(key):
                return str(msg[key])
        # price_change may nest
        changes = msg.get("price_changes") or msg.get("priceChanges")
        if isinstance(changes, list) and changes:
            row = changes[0]
            if isinstance(row, dict) and row.get("asset_id"):
                return str(row["asset_id"])
        return None

    async def _handle_raw(
        self, raw: str | bytes, dispatcher: EventDispatcher, *, generation: int
    ) -> None:
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
        _ = generation
        _ = time.perf_counter_ns()
        _ = self._sequencer.next()
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            asset_id = self._asset_id_from_msg(msg)
            if asset_id is not None and asset_id not in self._allowed:
                self.rejected_wrong_token += 1
                self._health(
                    "rejected_wrong_token",
                    asset_id=asset_id,
                    allowed=sorted(self._allowed),
                    window_slug=self._window_slug,
                )
                continue
            if self._market_id is not None:
                msg_market = msg.get("market") or msg.get("condition_id")
                if msg_market and str(msg_market) not in {
                    self._market_id.value,
                    str(self._market_id),
                }:
                    # Some book messages omit market; only reject when present+mismatch
                    self._health("rejected_wrong_market", market=str(msg_market))
                    continue
            event = normalize_market_ws_message(
                msg,
                ts_received=ts_received,
                correlation_id=self._correlation_id,
                market_id=self._market_id,
            )
            if event is None:
                continue
            if self._publish_events:
                dispatcher.publish(event)
            elif self._session_role is DiscoverySessionRole.PREPARED_NEXT:
                # Prepared-next: keep socket warm; do not publish as active books
                self._health("prepared_next_message", event_type=type(event).__name__)
