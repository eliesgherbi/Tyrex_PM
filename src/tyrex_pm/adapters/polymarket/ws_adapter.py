"""Live Polymarket CLOB market WebSocket adapter via official polymarket-client."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from tyrex_pm.adapters.polymarket.sdk_public import (
    build_async_public_client,
    sdk_market_event_to_tyrex,
)
from tyrex_pm.core.ids import CorrelationId, MarketId, new_correlation_id
from tyrex_pm.core.ingress import ConnectionGeneration, FeedRole
from tyrex_pm.domain.polymarket.discovery_binding import (
    DiscoveredMarketBinding,
    DiscoverySessionRole,
)
from tyrex_pm.engine.dispatcher import EventDispatcher

logger = logging.getLogger(__name__)

# Documented venue URL (SDK owns the actual connection).
MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class PolymarketMarketWsAdapter:
    """Subscribe to CLOB market books by validated token IDs only (official SDK)."""

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
        stream_factory: Callable[[Sequence[str]], Any] | None = None,
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
        self._ssl = ssl  # retained for call-site compatibility; SDK owns TLS
        self._stream_factory = stream_factory
        self._stop = asyncio.Event()
        self._stream = None
        self._conn_gen = ConnectionGeneration()
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
        if self._stream is not None:
            close = getattr(self._stream, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass
            self._stream = None

    def _health(self, status: str, **extra: object) -> None:
        if self._on_health:
            self._on_health(
                status,
                {
                    "adapter": "polymarket_clob_sdk",
                    "role": FeedRole.MARKET_BOOK.value,
                    "session_role": self._session_role.value,
                    "window_slug": self._window_slug,
                    "publish_events": self._publish_events,
                    **extra,
                },
            )

    async def _open_stream(self) -> Any:
        if self._stream_factory is not None:
            return await self._stream_factory(self._asset_ids)
        from polymarket.streams._specs import MarketSpec

        client = build_async_public_client()
        await client.__aenter__()
        try:
            handle = await client.subscribe(MarketSpec(token_ids=list(self._asset_ids)))
        except Exception:
            await client.close()
            raise

        class _Owned:
            def __init__(self) -> None:
                self._handle = handle
                self._client = client

            def __aiter__(self):
                return self._handle.__aiter__()

            async def close(self) -> None:
                try:
                    await self._handle.close()
                finally:
                    await self._client.close()

        return _Owned()

    async def run(self, dispatcher: EventDispatcher) -> None:
        backoff = self._reconnect_backoff_s
        while not self._stop.is_set():
            session_failed = False
            try:
                self.ready = False
                self._health("connecting", url=self._url, transport="polymarket-client")
                stream = await self._open_stream()
                self._stream = stream
                gen = self._conn_gen.bump()
                self._health(
                    "subscribed",
                    assets=self._asset_ids,
                    generation=gen,
                    transport="polymarket-client",
                )
                backoff = self._reconnect_backoff_s
                async for sdk_event in stream:
                    if self._stop.is_set():
                        break
                    await self._handle_sdk_event(sdk_event, dispatcher, generation=gen)
                    if not self.ready:
                        self.ready = True
                        self._health("ready", generation=gen)
                if not self._stop.is_set():
                    session_failed = True
                    self.ready = False
                    self._health(
                        "reconnecting",
                        error="connection_closed",
                        message="sdk stream ended",
                        generation=gen,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                session_failed = True
                self.ready = False
                self._health("reconnecting", error=type(exc).__name__, message=str(exc))
            finally:
                if self._stream is not None:
                    close = getattr(self._stream, "close", None)
                    if close is not None:
                        try:
                            await close()
                        except Exception:
                            pass
                self._stream = None
                self.ready = False
            if self._stop.is_set():
                break
            if session_failed:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff_s)
        self._health("disconnected")

    async def _handle_sdk_event(
        self, sdk_event: Any, dispatcher: EventDispatcher, *, generation: int
    ) -> None:
        _ = generation
        ts_received = datetime.now(timezone.utc)
        try:
            event = sdk_market_event_to_tyrex(
                sdk_event,
                ts_received=ts_received,
                correlation_id=self._correlation_id,
                market_id=self._market_id,
            )
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("sdk market event normalize failed: %s", exc)
            return
        if event is None:
            return
        asset_id = None
        from tyrex_pm.core.book_events import (
            BookDeltaReceived,
            BookSnapshotReceived,
            TickSizeChanged,
        )

        if isinstance(event, BookSnapshotReceived):
            asset_id = event.book.instrument_id.value
        elif isinstance(event, TickSizeChanged):
            asset_id = event.instrument_id.value
        elif isinstance(event, BookDeltaReceived) and event.changes:
            asset_id = event.changes[0].instrument_id.value
        if asset_id is not None and asset_id not in self._allowed:
            self.rejected_wrong_token += 1
            self._health(
                "rejected_wrong_token",
                asset_id=asset_id,
                allowed=sorted(self._allowed),
                window_slug=self._window_slug,
            )
            return
        if self._publish_events:
            dispatcher.publish(event)
        elif self._session_role is DiscoverySessionRole.PREPARED_NEXT:
            self._health("prepared_next_message", event_type=type(event).__name__)
