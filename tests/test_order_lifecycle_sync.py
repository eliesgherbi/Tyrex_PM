from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import ClientOrderId, IntentId, RunId, TokenId, VenueOrderId
from tyrex_pm.core.models import ApprovedIntent, EnterIntent, OpenOrderView
from tyrex_pm.execution.order_lifecycle import register_submit, sync_local_open_orders_from_venue_wallet
from tyrex_pm.state.order_store import LocalOrder, OrderStore
from tyrex_pm.state.reconcile import reconcile_open_orders
from tyrex_pm.state.wallet_store import WalletStore


def test_sync_local_remaining_to_venue_rounded_size_stops_false_size_mismatch() -> None:
    """Venue REST often reports fewer decimals than local notional math (e.g. 49.99 vs 49.999...)."""
    vid = VenueOrderId("0xabc")
    wallet = WalletStore()
    wallet.open_orders = (
        OpenOrderView(
            token_id=TokenId("t"),
            side=Side.BUY,
            remaining_size=Decimal("49.99"),
            limit_price=Decimal("0.08"),
            client_order_id=None,
            venue_order_id=vid,
            original_size=Decimal("49.99"),
            size_matched=Decimal("0"),
        ),
    )
    orders = OrderStore()
    cid = ClientOrderId("c1")
    orders.orders[cid] = LocalOrder(
        client_order_id=cid,
        venue_order_id=vid,
        token_id=TokenId("t"),
        side=Side.BUY,
        remaining=Decimal("49.99999993017446259751211361"),
    )
    r0 = reconcile_open_orders(wallet, orders)
    assert "open_order_size_mismatch" in r0.drift_flags
    assert "open_order_size_mismatch" in r0.blocking_drift_flags
    sync_local_open_orders_from_venue_wallet(orders, wallet)
    res = reconcile_open_orders(wallet, orders)
    assert res.drift_flags == ()
    assert res.blocking_drift_flags == ()
    assert orders.orders[cid].remaining == Decimal("49.99")


def test_register_submit_creates_provisional_local_order() -> None:
    orders = OrderStore()
    ap = ApprovedIntent(
        intent=EnterIntent(
            token_id=TokenId("tok"),
            side=Side.BUY,
            size=Decimal("5"),
            limit_price=Decimal("0.4"),
            order_style=OrderStyle.GTC,
            intent_id=IntentId("i1"),
        ),
        client_order_id=ClientOrderId("c-reg"),
        run_id=RunId("r1"),
    )
    register_submit(orders, ap)
    lo = orders.orders[ClientOrderId("c-reg")]
    assert lo.confirmation == "provisional"
    assert lo.venue_order_id is None
    assert lo.remaining == Decimal("5")


def test_sync_local_drops_local_when_merged_row_has_zero_remaining() -> None:
    wallet = WalletStore()
    vid = VenueOrderId("v0")
    wallet.open_orders = (
        OpenOrderView(
            token_id=TokenId("t"),
            side=Side.BUY,
            remaining_size=Decimal("0"),
            limit_price=Decimal("0.5"),
            client_order_id=None,
            venue_order_id=vid,
            original_size=Decimal("10"),
            size_matched=Decimal("10"),
            venue_state_source="user_ws",
        ),
    )
    orders = OrderStore()
    cid = ClientOrderId("c0")
    orders.orders[cid] = LocalOrder(
        client_order_id=cid,
        venue_order_id=vid,
        token_id=TokenId("t"),
        side=Side.BUY,
        remaining=Decimal("0"),
        confirmation="venue_confirmed",
    )
    sync_local_open_orders_from_venue_wallet(orders, wallet)
    assert cid not in orders.orders
