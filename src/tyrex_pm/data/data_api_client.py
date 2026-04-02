"""
HTTP client for Polymarket Data API (`https://data-api.polymarket.com`).

See: https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets
Rate limits: https://docs.polymarket.com/quickstart/introduction/rate-limits
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Any

import httpx

LogFn = Callable[..., None]

_logger = logging.getLogger(__name__)


class PolymarketDataApiClient:
    def __init__(
        self,
        base_url: str = "https://data-api.polymarket.com",
        *,
        timeout: float = 30.0,
        max_retries: int = 4,
        log_backoff: LogFn | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._log_backoff = log_backoff

    def get_trades(
        self,
        *,
        user: str,
        limit: int = 100,
        offset: int = 0,
        taker_only: bool = False,
    ) -> list[dict[str, Any]]:
        params: dict[str, str | int | bool] = {
            "user": user,
            "limit": limit,
            "offset": offset,
            "takerOnly": taker_only,
        }
        url = f"{self._base}/trades"
        attempt = 0
        while True:
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.get(url, params=params)
            except httpx.RequestError as exc:
                attempt += 1
                if attempt > self._max_retries:
                    raise
                self._backoff_sleep(attempt, retry_after_sec=None, detail=str(exc))
                continue

            if resp.status_code == 429:
                attempt += 1
                if attempt > self._max_retries:
                    resp.raise_for_status()
                ra = resp.headers.get("Retry-After")
                retry_after = float(ra) if ra and ra.isdigit() else None
                self._backoff_sleep(attempt, retry_after_sec=retry_after, detail="http_429")
                continue

            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError(f"unexpected /trades payload type: {type(data).__name__}")
            return data

    def get_user_trade_activity(
        self,
        *,
        user: str,
        limit: int = 200,
        offset: int = 0,
        start_ts_sec: int | None = None,
        end_ts_sec: int | None = None,
        sort_direction: str = "ASC",
        sort_by: str = "TIMESTAMP",
    ) -> list[dict[str, Any]]:
        """
        ``GET /activity`` filtered to TRADE rows — incremental guru polling.

        ``start_ts_sec`` / ``end_ts_sec`` are Unix **seconds** (API schema).
        See: https://docs.polymarket.com/api-reference/core/get-user-activity
        """

        if not (0 <= limit <= 500):
            raise ValueError("activity limit must be between 0 and 500")
        if offset < 0 or offset > 10000:
            raise ValueError("activity offset out of supported range")
        if sort_direction not in ("ASC", "DESC"):
            raise ValueError("sort_direction must be ASC or DESC")
        if sort_by not in ("TIMESTAMP", "TOKENS", "CASH"):
            raise ValueError("invalid sort_by")

        params: list[tuple[str, str | int | bool]] = [
            ("user", user),
            ("limit", limit),
            ("offset", offset),
            ("sortBy", sort_by),
            ("sortDirection", sort_direction),
            ("type", "TRADE"),
        ]
        if start_ts_sec is not None:
            params.append(("start", int(start_ts_sec)))
        if end_ts_sec is not None:
            params.append(("end", int(end_ts_sec)))

        url = f"{self._base}/activity"
        attempt = 0
        while True:
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.get(url, params=params)
            except httpx.RequestError as exc:
                attempt += 1
                if attempt > self._max_retries:
                    raise
                self._backoff_sleep(attempt, retry_after_sec=None, detail=str(exc))
                continue

            if resp.status_code == 429:
                attempt += 1
                if attempt > self._max_retries:
                    resp.raise_for_status()
                ra = resp.headers.get("Retry-After")
                retry_after = float(ra) if ra and ra.isdigit() else None
                self._backoff_sleep(attempt, retry_after_sec=retry_after, detail="http_429")
                continue

            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError(f"unexpected /activity payload type: {type(data).__name__}")
            return data  # type: ignore[return-value]

    def _backoff_sleep(self, attempt: int, *, retry_after_sec: float | None, detail: str) -> None:
        base = 2 ** min(attempt, 6)
        delay = min(60.0, base + random.random())
        if retry_after_sec is not None:
            delay = max(delay, retry_after_sec)
        if self._log_backoff:
            self._log_backoff(
                event="poller_backoff",
                attempt=attempt,
                sleep_s=round(delay, 3),
                retry_after=retry_after_sec,
                detail=detail,
            )
        else:
            _logger.warning(
                "event=poller_backoff attempt=%s sleep_s=%s retry_after=%s detail=%s",
                attempt,
                round(delay, 3),
                retry_after_sec,
                detail,
            )
        time.sleep(delay)
