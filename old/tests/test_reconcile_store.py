from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import ClientOrderId, TokenId, VenueOrderId
from tyrex_pm.core.models import OpenOrderView
from tyrex_pm.state.order_store import LocalOrder, OrderStore
from tyrex_pm.state.reconcile import reconcile_open_orders, remaining_sizes_equivalent
from tyrex_pm.state.wallet_store import WalletStore


def test_remaining_sizes_equivalent_near_equal_decimals() -> None:
    assert remaining_sizes_equivalent(Decimal("1"), Decimal("1"))
    assert remaining_sizes_equivalent(Decimal("1.0000000000000001"), Decimal("1"))
    assert not remaining_sizes_equivalent(Decimal("1"), Decimal("2"))


def test_reconcile_ok_partial_fill_original_minus_matched() -> None:
    w = WalletStore()
    w.open_orders = (
        OpenOrderView(
            token_id=TokenId("t"),
            side=Side.BUY,
            remaining_size=Decimal("7"),
            limit_price=Decimal("0.5"),
            client_order_id=None,
            venue_order_id=VenueOrderId("v1"),
            original_size=Decimal("10"),
            size_matched=Decimal("3"),
        ),
    )
    o = OrderStore()
    cid = ClientOrderId("x")
    o.orders[cid] = LocalOrder(
        client_order_id=cid,
        venue_order_id=VenueOrderId("v1"),
        token_id=TokenId("t"),
        side=Side.BUY,
        remaining=Decimal("7"),
    )
    res = reconcile_open_orders(w, o)
    assert res.drift_flags == ()
    assert res.blocking_drift_flags == ()


def test_reconcile_size_only_field_treated_as_remaining() -> None:
    w = WalletStore()
    w.open_orders = (
        OpenOrderView(
            token_id=TokenId("t"),
            side=Side.SELL,
            remaining_size=Decimal("4.5"),
            limit_price=Decimal("0.2"),
            client_order_id=None,
            venue_order_id=VenueOrderId("v2"),
            original_size=None,
            size_matched=None,
        ),
    )
    o = OrderStore()
    cid = ClientOrderId("y")
    o.orders[cid] = LocalOrder(
        client_order_id=cid,
        venue_order_id=VenueOrderId("v2"),
        token_id=TokenId("t"),
        side=Side.SELL,
        remaining=Decimal("4.5"),
    )
    res = reconcile_open_orders(w, o)
    assert res.drift_flags == ()
    assert res.blocking_drift_flags == ()


def test_reconcile_open_order_size_mismatch_emits_comparison() -> None:
    w = WalletStore()
    w.open_orders = (
        OpenOrderView(
            token_id=TokenId("t"),
            side=Side.BUY,
            remaining_size=Decimal("9"),
            limit_price=Decimal("0.5"),
            client_order_id=None,
            venue_order_id=VenueOrderId("v3"),
            original_size=Decimal("10"),
            size_matched=Decimal("1"),
        ),
    )
    o = OrderStore()
    cid = ClientOrderId("z")
    o.orders[cid] = LocalOrder(
        client_order_id=cid,
        venue_order_id=VenueOrderId("v3"),
        token_id=TokenId("t"),
        side=Side.BUY,
        remaining=Decimal("5"),
    )
    res = reconcile_open_orders(w, o)
    assert "open_order_size_mismatch" in res.drift_flags
    assert "open_order_size_mismatch" in res.blocking_drift_flags
    assert res.order_comparisons
    row = res.order_comparisons[0]
    assert row["venue_order_id"] == "v3"
    assert row["local_remaining"] == "5"
    assert row["venue_remaining"] == "9"
    assert row["venue_original_size"] == "10"
    assert row["venue_size_matched"] == "1"
    assert row["venue_remaining_computed"] == "9"
    assert row["remaining_match"] is False
    assert row["row_blocks_live"] is True
    assert row["local_confirmation"] == "provisional"


def test_reconcile_local_open_not_on_venue() -> None:
    w = WalletStore()
    o = OrderStore()
    cid = ClientOrderId("a")
    o.orders[cid] = LocalOrder(
        client_order_id=cid,
        venue_order_id=VenueOrderId("missing"),
        token_id=TokenId("t"),
        side=Side.BUY,
        remaining=Decimal("1"),
    )
    res = reconcile_open_orders(w, o)
    assert "local_open_not_on_venue" in res.drift_flags
    assert "local_open_not_on_venue" in res.blocking_drift_flags


def test_reconcile_flags_when_local_has_extra_open_order() -> None:
    w = WalletStore()
    o = OrderStore()
    cid = ClientOrderId("local-only")
    o.orders[cid] = LocalOrder(
        client_order_id=cid,
        venue_order_id=None,
        token_id=TokenId("1"),
        side=Side.BUY,
        remaining=Decimal("1"),
    )
    res = reconcile_open_orders(w, o)
    assert "local_orders_missing_venue_excess" in res.drift_flags
    assert "local_orders_missing_venue_excess" in res.blocking_drift_flags


def test_reconcile_ok_when_both_empty() -> None:
    w = WalletStore()
    o = OrderStore()
    res = reconcile_open_orders(w, o)
    assert res.drift_flags == ()
    assert res.blocking_drift_flags == ()
