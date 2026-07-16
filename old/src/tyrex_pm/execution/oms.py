from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from tyrex_pm.core.models import ApprovedCancel, ApprovedIntent
from tyrex_pm.execution.adapters import OMSBackend


@dataclass(frozen=True)
class _Place:
    ap: ApprovedIntent
    #: Phase 5: optional resolved :class:`MarketInfo` for ``ap.intent.token_id``,
    #: passed through to the backend so the order builder can tick-quantize the
    #: limit price. Carried on the queue item rather than as a side-channel so
    #: the single-writer ordering still applies (no out-of-band state).
    market_info: Any | None = None


@dataclass(frozen=True)
class _Cancel:
    ac: ApprovedCancel


class SingleWriterOMS:
    """Serialize submit + cancel onto one asyncio queue (single-writer per wallet)."""

    def __init__(self, backend: OMSBackend) -> None:
        self._backend = backend
        self._q: asyncio.Queue[tuple[_Place | _Cancel, asyncio.Future[str]]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            item, fut = await self._q.get()
            try:
                if isinstance(item, _Place):
                    if item.market_info is not None:
                        res = await self._backend.submit(item.ap, market_info=item.market_info)
                    else:
                        res = await self._backend.submit(item.ap)
                else:
                    res = await self._backend.cancel(item.ac)
                if not fut.done():
                    fut.set_result(res)
            except Exception as e:
                if not fut.done():
                    fut.set_exception(e)
            finally:
                self._q.task_done()

    async def submit(
        self,
        ap: ApprovedIntent,
        *,
        market_info: Any | None = None,
    ) -> str:
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        await self._q.put((_Place(ap, market_info=market_info), fut))
        return await fut

    async def cancel(self, ac: ApprovedCancel) -> str:
        fut = asyncio.get_running_loop().create_future()
        await self._q.put((_Cancel(ac), fut))
        return await fut
