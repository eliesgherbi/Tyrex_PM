from __future__ import annotations

from tyrex_pm.core.models import ApprovedCancel, ApprovedIntent
from tyrex_pm.execution.order_builder import to_place_request
from tyrex_pm.venue.polymarket.clob_bridge import PyClobBridge, summarize_oms_response


class LiveOMS:
    """Single-writer live path: signed create+post via py-clob-client."""

    def __init__(self, bridge: PyClobBridge) -> None:
        self._bridge = bridge

    async def submit(self, ap: ApprovedIntent) -> str:
        req = to_place_request(ap)
        resp = await self._bridge.create_and_post_limit(req)
        return summarize_oms_response(resp)

    async def cancel(self, ac: ApprovedCancel) -> str:
        if ac.venue_order_id is None:
            raise ValueError("Live cancel requires venue_order_id")
        resp = await self._bridge.cancel_order(ac.venue_order_id)
        return summarize_oms_response(resp)
