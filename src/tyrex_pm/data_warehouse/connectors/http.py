from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from typing import Any
from urllib.parse import quote

from ..config import Operations
from ..contracts import canonical, decode

ENDPOINTS = {
    "gamma_keyset": ("GET", "https://gamma-api.polymarket.com/markets/keyset", "gamma"),
    "gamma_markets": ("GET", "https://gamma-api.polymarket.com/markets", "gamma"),
    "gamma_events": ("GET", "https://gamma-api.polymarket.com/events", "gamma"),
    "clob_books": ("POST", "https://clob.polymarket.com/books", "clob"),
    "clob_market": ("GET", "https://clob.polymarket.com/clob-markets/", "clob"),
    "clob_time": ("GET", "https://clob.polymarket.com/time", "clob"),
    "resolutions": ("GET", "https://data-api.polymarket.com/v2/resolutions", "resolution"),
    "binance_depth": ("GET", "https://api.binance.com/api/v3/depth", "binance"),
    "binance_time": ("GET", "https://api.binance.com/api/v3/time", "binance"),
}


class Budget:
    def __init__(self, rate: float, burst: int):
        self.rate, self.burst = rate, burst
        self.tokens = float(burst)
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self, weight: int = 1) -> None:
        if weight > self.burst:
            raise ValueError("request weight exceeds limiter capacity")
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= weight:
                    self.tokens -= weight
                    return
                await asyncio.sleep((weight - self.tokens) / self.rate)


class PublicHttp:
    def __init__(self, capture: Any, operations: Operations, *, transport: Any = None):
        import httpx

        self.capture, self.operations = capture, operations
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(operations.read_timeout_s, connect=operations.connect_timeout_s),
            follow_redirects=False,
            transport=transport,
        )
        self.budgets = defaultdict(
            lambda: Budget(operations.requests_per_second, operations.request_burst)
        )
        self.weights = Budget(
            operations.binance_weight_per_minute / 60, operations.binance_weight_per_minute
        )

    async def read(
        self,
        endpoint: str,
        *,
        params: dict | None = None,
        body: Any = None,
        connection: str | None = None,
    ) -> tuple[Any, Any]:
        if endpoint not in ENDPOINTS:
            raise ValueError("endpoint not in public read-only allowlist")
        method, url, source = ENDPOINTS[endpoint]
        params = dict(params or {})
        if endpoint == "clob_market":
            condition = str(params.get("condition", ""))
            if not condition or len(condition) > 200:
                raise ValueError("invalid condition selector")
            url += quote(condition, safe="")
            wire_params = None
        else:
            wire_params = params
        if endpoint == "clob_books" and (not isinstance(body, list) or not 1 <= len(body) <= 500):
            raise ValueError("book request requires 1..500 selectors")
        if endpoint == "resolutions" and len(str(params.get("condition", "")).split(",")) > 20:
            raise ValueError("resolution batch exceeds 20 conditions")
        # Related Polymarket hosts share a conservative collector quota.
        group = "binance" if source == "binance" else "polymarket"
        await self.budgets[group].acquire()
        if source == "binance":
            await self.weights.acquire(250 if endpoint == "binance_depth" else 1)
        started = time.time_ns()
        request_id = uuid.uuid4().hex
        response = await self.client.request(
            method, url, params=wire_params, json=body if method == "POST" else None
        )
        arrived = time.time_ns()
        headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower() in ("date", "retry-after", "x-mbx-used-weight-1m", "content-type")
        }
        record = await self.capture(
            response.content,
            source=source,
            stream=endpoint,
            connection=connection,
            transport="http",
            request_id=request_id,
            method=method,
            endpoint_id=endpoint,
            query_redacted=canonical({**params, "body": body} if body else params),
            http_status=response.status_code,
            content_type=response.headers.get("content-type"),
            request_started_at=started,
            response_headers=canonical(headers),
            received_at=arrived,
        )
        if response.status_code == 429:
            try:
                delay = min(60, max(1, float(headers.get("retry-after", "5"))))
            except ValueError:
                delay = 5
            await asyncio.sleep(delay)
        if not 200 <= response.status_code < 300:
            raise ValueError(f"public_read_failed:{endpoint}:{response.status_code}")
        return decode(response.content), record

    async def close(self) -> None:
        await self.client.aclose()
