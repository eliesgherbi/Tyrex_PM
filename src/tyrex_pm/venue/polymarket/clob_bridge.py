from __future__ import annotations

import asyncio
import json
from typing import Any

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import VenueOrderId
from tyrex_pm.venue.polymarket.clob_execution import PlaceOrderRequest


class PyClobBridge:
    """Async wrapper around synchronous py-clob-client ClobClient."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def create_and_post_limit(self, req: PlaceOrderRequest) -> dict[str, Any]:
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import BUY, SELL

        side = BUY if req.side == Side.BUY else SELL
        oa = OrderArgs(
            token_id=str(req.token_id),
            price=float(req.price),
            size=float(req.size),
            side=side,
        )

        def _run() -> dict[str, Any]:
            raw = self._client.create_and_post_order(oa)
            if isinstance(raw, dict):
                return raw
            if raw is None:
                return {}
            return {"raw": raw}

        return await asyncio.to_thread(_run)

    async def cancel_order(self, venue_order_id: VenueOrderId) -> dict[str, Any]:
        vid = str(venue_order_id)

        def _run() -> dict[str, Any]:
            raw = self._client.cancel(vid)
            if isinstance(raw, dict):
                return raw
            return {"raw": raw}

        return await asyncio.to_thread(_run)

    async def post_heartbeat(self, heartbeat_id: str) -> dict[str, Any]:
        # Send server-provided ids verbatim (docs/examples use "" first, then exact returned id).
        hid = "" if heartbeat_id is None else str(heartbeat_id).strip()

        def _run() -> dict[str, Any]:
            raw = self._client.post_heartbeat(hid)
            if isinstance(raw, dict):
                return raw
            return {"raw": raw}

        return await asyncio.to_thread(_run)


def summarize_oms_response(resp: dict[str, Any]) -> str:
    return json.dumps(resp, default=str)


def parse_venue_order_id(resp: dict[str, Any]) -> VenueOrderId | None:
    oid = resp.get("orderID") or resp.get("order_id") or resp.get("id")
    if not oid:
        return None
    return VenueOrderId(str(oid))
