from __future__ import annotations

import time
from typing import Any

import httpx

from tyrex_pm.core import reason_codes as rc


def _market_tradeable(m: dict[str, Any]) -> tuple[bool, str | None]:
    if m.get("closed") is True:
        return False, rc.MARKET_UNTRADEABLE
    if m.get("archived") is True:
        return False, rc.MARKET_UNTRADEABLE
    if m.get("active") is False:
        return False, rc.MARKET_UNTRADEABLE
    if m.get("acceptingOrders") is False:
        return False, rc.MARKET_UNTRADEABLE
    return True, None


class GammaClient:
    """Gamma API — market metadata by clob token id (canonical outcome token)."""

    def __init__(self, base_url: str = "https://gamma-api.polymarket.com") -> None:
        self._base = base_url.rstrip("/")
        self._cache: dict[str, tuple[bool, str | None, float]] = {}
        self._ttl_s = 60.0

    async def is_token_tradeable(
        self,
        client: httpx.AsyncClient,
        token_id: str,
        *,
        now_s: float | None = None,
    ) -> tuple[bool, str | None]:
        tid = str(token_id)
        t = now_s if now_s is not None else time.monotonic()
        hit = self._cache.get(tid)
        if hit is not None:
            ok, reason, exp = hit
            if t < exp:
                return ok, reason

        try:
            r = await client.get(
                f"{self._base}/markets",
                params=[("clob_token_ids", tid)],
                timeout=30.0,
            )
            r.raise_for_status()
            rows = r.json()
        except Exception:
            self._cache[tid] = (False, rc.MARKET_METADATA_UNAVAILABLE, t + self._ttl_s)
            return False, rc.MARKET_METADATA_UNAVAILABLE

        if not isinstance(rows, list) or not rows:
            self._cache[tid] = (False, rc.MARKET_METADATA_UNAVAILABLE, t + self._ttl_s)
            return False, rc.MARKET_METADATA_UNAVAILABLE

        m = rows[0]
        if not isinstance(m, dict):
            self._cache[tid] = (False, rc.MARKET_METADATA_UNAVAILABLE, t + self._ttl_s)
            return False, rc.MARKET_METADATA_UNAVAILABLE

        ok, reason = _market_tradeable(m)
        self._cache[tid] = (ok, reason, t + self._ttl_s)
        return ok, reason
