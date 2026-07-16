from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId, VenueOrderId
from tyrex_pm.core.models import OpenOrderView
from tyrex_pm.ingestion.user_stream import apply_user_ws_message
from tyrex_pm.state.wallet_store import WalletStore


def test_user_ws_placement_merges_open_order() -> None:
    w = WalletStore()
    apply_user_ws_message(
        w,
        {
            "type": "PLACEMENT",
            "id": "0xorder1",
            "asset_id": "123",
            "side": "BUY",
            "original_size": "10",
            "size_matched": "0",
            "price": "0.55",
        },
    )
    assert len(w.open_orders) == 1
    o = w.open_orders[0]
    assert o.venue_order_id == VenueOrderId("0xorder1")
    assert o.remaining_size == Decimal("10")
    assert o.venue_state_source == "user_ws"


def test_user_ws_cancellation_drops_order() -> None:
    w = WalletStore()
    w.user_ws_upsert_order(
        OpenOrderView(
            token_id=TokenId("123"),
            side=Side.BUY,
            remaining_size=Decimal("1"),
            limit_price=Decimal("0.5"),
            client_order_id=None,
            venue_order_id=VenueOrderId("0xorder1"),
        )
    )
    apply_user_ws_message(w, {"type": "CANCELLATION", "id": "0xorder1"})
    assert w.open_orders == ()
