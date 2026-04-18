from __future__ import annotations

import logging
from typing import Callable, Coroutine, Any

log = logging.getLogger(__name__)


class ClobHeartbeat:
    """
    Supervised keep-alive for CLOB session (Phase 11).
    On failure, caller must set health degraded.
    """

    def __init__(
        self,
        *,
        ping: Callable[[], Coroutine[Any, Any, None]],
        interval_s: float = 30.0,
    ) -> None:
        self._ping = ping
        self._interval_s = interval_s

    async def run_once(self) -> bool:
        try:
            await self._ping()
            return True
        except Exception:
            log.exception("heartbeat ping failed")
            return False
