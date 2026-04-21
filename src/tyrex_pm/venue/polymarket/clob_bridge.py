"""
Async wrapper around the synchronous ``py-clob-client-v2`` ``ClobClient``.

Phase 2 (V2 bridge) — translates Tyrex's internal ``PlaceOrderRequest`` /
``VenueOrderId`` into V2 SDK calls and returns the venue's JSON response in a
shape compatible with ``parse_venue_order_id``.

The bridge is intentionally thin: it does not own market metadata (tick size /
neg-risk are auto-resolved inside ``client.create_order`` by REST calls to the
venue; Phase 5 — the market-info adapter — will hoist that into a cache and
pass explicit ``PartialCreateOrderOptions``).

V2 SDK surface used here:

* ``py_clob_client_v2.OrderArgsV2`` — V2 order dataclass. No V1 ``fee_rate_bps``,
  ``nonce``, or ``taker`` fields. ``side`` is the bare string ``"BUY"`` /
  ``"SELL"``.
* ``py_clob_client_v2.OrderType`` — ``GTC`` / ``GTD`` / ``FOK`` / ``FAK`` are
  plain string class attributes (e.g. ``OrderType.GTC == "GTC"``).
* ``py_clob_client_v2.OrderPayload`` — single-field ``orderID: str`` envelope
  used for ``ClobClient.cancel_order``.
* ``py_clob_client_v2.order_builder.constants.BUY`` / ``SELL`` — string
  constants accepted by ``OrderArgsV2.side``.
* ``ClobClient.create_and_post_order(order_args, order_type=…)`` — single combined
  call replacing V1's ``create_order`` + ``post_order``.
* ``ClobClient.cancel_order(OrderPayload(orderID=…))`` — single-order cancel.
* Builder code, when configured on the client via Phase 1's ``BuilderConfig``,
  is auto-applied inside ``ClobClient.create_order``; the bridge does not need
  to set it on every ``OrderArgsV2``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import VenueOrderId
from tyrex_pm.venue.polymarket.clob_execution import PlaceOrderRequest


def _v2_order_type(style: OrderStyle) -> str:
    """Map Tyrex ``OrderStyle`` → V2 ``OrderType`` string."""
    from py_clob_client_v2 import OrderType

    mapping = {
        OrderStyle.GTC: OrderType.GTC,
        OrderStyle.FOK: OrderType.FOK,
        OrderStyle.FAK: OrderType.FAK,
    }
    return mapping.get(style, OrderType.GTC)


def _v2_side(side: Side) -> str:
    """Map Tyrex ``Side`` → V2 BUY/SELL string constant."""
    from py_clob_client_v2.order_builder.constants import BUY, SELL

    return BUY if side == Side.BUY else SELL


class PyClobBridge:
    """Async wrapper around synchronous ``py-clob-client-v2`` ``ClobClient``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def create_and_post_limit(self, req: PlaceOrderRequest) -> dict[str, Any]:
        """Build a V2 ``OrderArgsV2`` from a Tyrex ``PlaceOrderRequest`` and post it.

        Tick size and neg-risk for the token are auto-resolved by the V2 SDK
        (one REST call each per submit). Phase 5's market-info adapter will
        pre-resolve and pass them via ``PartialCreateOrderOptions`` to drop
        those round-trips off the hot submit path.
        """
        from py_clob_client_v2 import OrderArgsV2

        order_args = OrderArgsV2(
            token_id=str(req.token_id),
            price=float(req.price),
            size=float(req.size),
            side=_v2_side(req.side),
        )
        order_type = _v2_order_type(req.style)

        def _run() -> dict[str, Any]:
            raw = self._client.create_and_post_order(order_args, order_type=order_type)
            if isinstance(raw, dict):
                return raw
            if raw is None:
                return {}
            return {"raw": raw}

        return await asyncio.to_thread(_run)

    async def cancel_order(self, venue_order_id: VenueOrderId) -> dict[str, Any]:
        """Cancel a single V2 order by venue order id (a.k.a. ``orderID``)."""
        from py_clob_client_v2 import OrderPayload

        payload = OrderPayload(orderID=str(venue_order_id))

        def _run() -> dict[str, Any]:
            raw = self._client.cancel_order(payload)
            if isinstance(raw, dict):
                return raw
            return {"raw": raw}

        return await asyncio.to_thread(_run)

    async def post_heartbeat(self, heartbeat_id: str) -> dict[str, Any]:
        """Heartbeat path is unchanged in V2 (``POST /v1/heartbeats`` with empty
        string on first call, then the venue-supplied id verbatim).
        """
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
    """Extract the venue order id from a V2 ``post_order`` response.

    V2 keeps the V1 envelope shape: the venue returns ``{"orderID": "0x…", …}``
    on success (the SDK serializes our request body with the ``orderID`` key
    via ``order_to_json_v2``, and the venue echoes the same key in the
    response body). Defensive fallbacks (``order_id`` / ``id``) are kept in
    case the venue surface adds variants.
    """
    oid = resp.get("orderID") or resp.get("order_id") or resp.get("id")
    if not oid:
        return None
    return VenueOrderId(str(oid))
