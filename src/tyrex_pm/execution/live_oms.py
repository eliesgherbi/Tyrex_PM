"""Live OMS: signed create+post via py-clob-client-v2.

Phase 5 (market-info adapter) extended :meth:`LiveOMS.submit` to accept an
optional :class:`MarketInfo` for the order's token. When supplied, it is used
to tick-quantize the price at the OMS boundary (see
:func:`tyrex_pm.execution.order_builder.to_place_request`). The pipeline that
calls into ``submit`` is responsible for resolving market info via
:class:`tyrex_pm.venue.polymarket.market_info.MarketInfoCache` *before* this
method is invoked; ``LiveOMS`` does no caching of its own.
"""

from __future__ import annotations

from typing import Any

from tyrex_pm.core.models import ApprovedCancel, ApprovedIntent
from tyrex_pm.execution.order_builder import to_place_request
from tyrex_pm.venue.polymarket.clob_bridge import PyClobBridge, summarize_oms_response


class LiveOMS:
    """Single-writer live path: signed create+post via py-clob-client-v2."""

    def __init__(self, bridge: PyClobBridge) -> None:
        self._bridge = bridge

    async def submit(
        self,
        ap: ApprovedIntent,
        *,
        market_info: Any | None = None,
    ) -> str:
        req = to_place_request(ap, market_info=market_info)
        resp = await self._bridge.create_and_post_limit(req)
        return summarize_oms_response(resp)

    async def cancel(self, ac: ApprovedCancel) -> str:
        if ac.venue_order_id is None:
            raise ValueError("Live cancel requires venue_order_id")
        resp = await self._bridge.cancel_order(ac.venue_order_id)
        return summarize_oms_response(resp)
