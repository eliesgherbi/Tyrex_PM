from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import ClientOrderId, TokenId, VenueOrderId


@dataclass
class PlaceOrderRequest:
    token_id: TokenId
    side: Side
    size: Decimal
    price: Decimal
    style: OrderStyle
    client_order_id: ClientOrderId


class ClobExecutionClient:
    """Live CLOB HTTP — stub until Phase 11 wires py-clob / official client."""

    async def place_order(self, req: PlaceOrderRequest) -> dict[str, Any]:
        raise NotImplementedError("Live CLOB in Phase 11")

    async def cancel_order(self, venue_order_id: VenueOrderId) -> dict[str, Any]:
        raise NotImplementedError("Live CLOB in Phase 11")
