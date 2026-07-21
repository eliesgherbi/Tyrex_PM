"""Generic multi-feed supervisor for read-only public adapters (N2).

One-run and continuous modes share the same adapters; this helper only
owns connect/stop/aggregation — not Z-Gap session promotion (N4).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from tyrex_pm.core.ingress import FeedReadiness
from tyrex_pm.engine.dispatcher import EventDispatcher

logger = logging.getLogger(__name__)

HealthCallback = Callable[[str, dict], None]


@dataclass
class FeedHandle:
    name: str
    adapter: Any
    required_for_ready: bool = True


@dataclass
class AggregatedFeedReadiness:
    feeds: dict[str, FeedReadiness] = field(default_factory=dict)
    overall: FeedReadiness = FeedReadiness.NOT_READY
    detail: str | None = None


class FeedSupervisor:
    """Run multiple MarketDataAdapter-like objects until stop."""

    def __init__(
        self,
        feeds: list[FeedHandle],
        *,
        on_health: HealthCallback | None = None,
    ) -> None:
        self._feeds = feeds
        self._on_health = on_health
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()

    def readiness(self) -> AggregatedFeedReadiness:
        feeds: dict[str, FeedReadiness] = {}
        for handle in self._feeds:
            ready = bool(getattr(handle.adapter, "ready", False))
            feeds[handle.name] = FeedReadiness.READY if ready else FeedReadiness.NOT_READY
        required = [h for h in self._feeds if h.required_for_ready]
        if not required:
            overall = FeedReadiness.READY
            detail = None
        elif all(feeds[h.name] is FeedReadiness.READY for h in required):
            overall = FeedReadiness.READY
            detail = None
        else:
            missing = [h.name for h in required if feeds[h.name] is not FeedReadiness.READY]
            # Settlement stale while trading fresh → visibly degraded
            if (
                feeds.get("rtds_chainlink") is not FeedReadiness.READY
                and feeds.get("binance_spot") is FeedReadiness.READY
            ):
                overall = FeedReadiness.DEGRADED
                detail = "settlement_stale_trading_fresh"
            else:
                overall = FeedReadiness.NOT_READY
                detail = "not_ready:" + ",".join(missing)
        return AggregatedFeedReadiness(feeds=feeds, overall=overall, detail=detail)

    async def run(self, dispatcher: EventDispatcher) -> None:
        self._tasks = [
            asyncio.create_task(self._run_one(handle, dispatcher), name=f"feed:{handle.name}")
            for handle in self._feeds
        ]
        self._health("supervisor_started", feeds=[h.name for h in self._feeds])
        try:
            await self._stop.wait()
        finally:
            await self.stop()

    async def _run_one(self, handle: FeedHandle, dispatcher: EventDispatcher) -> None:
        try:
            await handle.adapter.run(dispatcher)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("feed %s crashed: %s", handle.name, exc)
            self._health("feed_error", feed=handle.name, error=type(exc).__name__)

    async def stop(self) -> None:
        self._stop.set()
        for handle in self._feeds:
            stop = getattr(handle.adapter, "stop", None)
            if stop is not None:
                try:
                    await stop()
                except Exception as exc:
                    logger.warning("feed %s stop failed: %s", handle.name, exc)
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._health("supervisor_stopped")

    def _health(self, status: str, **extra: object) -> None:
        if self._on_health:
            self._on_health(status, dict(extra))
