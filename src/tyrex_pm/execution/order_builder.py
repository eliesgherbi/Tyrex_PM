from __future__ import annotations

from tyrex_pm.core.models import ApprovedIntent
from tyrex_pm.venue.polymarket.clob_execution import PlaceOrderRequest


def to_place_request(ap: ApprovedIntent) -> PlaceOrderRequest:
    i = ap.intent
    return PlaceOrderRequest(
        token_id=i.token_id,
        side=i.side,
        size=i.size,
        price=i.limit_price or 0,
        style=i.order_style,
        client_order_id=ap.client_order_id,
    )
