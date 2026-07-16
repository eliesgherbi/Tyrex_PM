from __future__ import annotations

from tyrex_pm.core.ids import VenueOrderId
from tyrex_pm.venue.polymarket.clob_execution import ClobExecutionClient


async def cancel_venue(clob: ClobExecutionClient, venue_order_id: VenueOrderId) -> dict:
    return await clob.cancel_order(venue_order_id)
