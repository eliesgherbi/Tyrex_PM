"""Minimal read-only adapter boundaries."""

from __future__ import annotations

from typing import Protocol

from tyrex_pm.domain.polymarket.market import BinaryMarket, MarketRequest
from tyrex_pm.engine.dispatcher import EventDispatcher


class MarketDiscovery(Protocol):
    async def resolve_market(self, request: MarketRequest) -> BinaryMarket: ...


class MarketDataAdapter(Protocol):
    """Publishes normalized book / reference events onto the dispatcher."""

    async def run(self, dispatcher: EventDispatcher) -> None: ...

    async def stop(self) -> None: ...
