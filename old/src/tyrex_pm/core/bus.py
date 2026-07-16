from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from tyrex_pm.core.models import EventEnvelope
from tyrex_pm.core.time import utc_now

log = logging.getLogger(__name__)

T = TypeVar("T")


class EventBus:
    """Minimal async pub/sub for internal events."""

    def __init__(self) -> None:
        self._subs: dict[type, list[Callable[[Any], Awaitable[None]]]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def subscribe(self, payload_type: type[T], handler: Callable[[EventEnvelope], Awaitable[None]]) -> None:
        self._subs[payload_type].append(handler)  # type: ignore[arg-type]

    async def publish(self, envelope: EventEnvelope) -> None:
        ptype = type(envelope.payload)
        handlers = list(self._subs.get(ptype, ()))
        for h in handlers:
            try:
                await h(envelope)
            except Exception:
                log.exception("bus handler failed for %s", ptype.__name__)

    def publish_nowait(self, envelope: EventEnvelope) -> asyncio.Task[None] | None:
        return asyncio.create_task(self.publish(envelope))

    @staticmethod
    def wrap(
        payload: Any,
        *,
        schema_version: int = 1,
        source: Any = None,
    ) -> EventEnvelope:
        from tyrex_pm.core.enums import EventSource
        from uuid import uuid4

        src = source if isinstance(source, EventSource) else EventSource.INTERNAL
        return EventEnvelope(
            event_id=uuid4(),
            schema_version=schema_version,
            ts_recv=utc_now(),
            source=src,
            payload=payload,
        )
