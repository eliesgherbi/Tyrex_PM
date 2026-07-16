from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from tyrex_pm.core.models import GuruTradeSignal
from tyrex_pm.venue.polymarket.normalizers import normalize_data_api_activity_row


# Default host — override in tests or config
DEFAULT_DATA_API_BASE = "https://data-api.polymarket.com"


@dataclass
class ActivityPage:
    """One page of activity; `cursor` is opaque next-page token from API."""

    items: list[dict[str, Any]]
    next_cursor: str | None


class DataApiClient:
    def __init__(
        self,
        base_url: str = DEFAULT_DATA_API_BASE,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(base_url=self._base, timeout=30.0)

    async def fetch_wallet_activity(
        self,
        proxy_wallet: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ActivityPage:
        """
        Fetch incremental activity for guru copy.

        Live path: GET /activity (or documented endpoint) with wallet + pagination.
        Exact query params follow Polymarket Data API — adjust when wiring production.
        """
        params: dict[str, Any] = {"user": proxy_wallet, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        c = await self._get_client()
        try:
            r = await c.get("/activity", params=params)
            r.raise_for_status()
            data = r.json()
        finally:
            if self._client is None:
                await c.aclose()

        if isinstance(data, list):
            return ActivityPage(items=list(data), next_cursor=None)
        if isinstance(data, dict):
            items = data.get("data") or data.get("activities") or data.get("items") or []
            nxt = data.get("next_cursor") or data.get("cursor")
            return ActivityPage(items=list(items), next_cursor=str(nxt) if nxt else None)
        return ActivityPage(items=[], next_cursor=None)

    async def fetch_positions(self, wallet_address: str) -> list[dict[str, Any]]:
        """Fetch the proxy/funder's outcome positions from Polymarket Data API.

        Returns the raw row list. Callers should normalize via :func:`normalize_position_rows`
        and feed the result into :func:`refresh_positions_into_wallet` so REST stays the
        authoritative source for ``WalletStore.positions`` (WS only delivers incremental
        CONFIRMED trade events and can drop messages across reconnects).
        """
        params: dict[str, Any] = {"user": wallet_address}
        c = await self._get_client()
        try:
            r = await c.get("/positions", params=params)
            r.raise_for_status()
            data = r.json()
        finally:
            if self._client is None:
                await c.aclose()
        if isinstance(data, list):
            return list(data)
        if isinstance(data, dict):
            rows = data.get("data") or data.get("positions") or data.get("items") or []
            return list(rows) if isinstance(rows, list) else []
        return []

    @staticmethod
    def parse_activity_json(text: str, guru_wallet: str) -> list[GuruTradeSignal]:
        """Fixture-first: parse JSON array or envelope for tests."""
        raw = json.loads(text)
        rows: list[dict[str, Any]]
        if isinstance(raw, list):
            rows = raw
        elif isinstance(raw, dict):
            rows = list(raw.get("activities") or raw.get("data") or raw.get("items") or [])
        else:
            rows = []

        out: list[GuruTradeSignal] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            sig = normalize_data_api_activity_row(row, guru_wallet)
            if sig is not None:
                out.append(sig)
        return out
