from __future__ import annotations

import asyncio
import time


class TokenBucket:
    def __init__(self, rate_per_s: float, capacity: float) -> None:
        self._rate = rate_per_s
        self._cap = capacity
        self._tokens = capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self._cap, self._tokens + elapsed * self._rate)
            if self._tokens < cost:
                need = cost - self._tokens
                wait = need / self._rate if self._rate > 0 else 0.1
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= cost
