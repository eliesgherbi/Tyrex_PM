from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import ClientOrderId, TokenId, VenueOrderId
from tyrex_pm.core.models import OpenOrderView
from tyrex_pm.ingestion.user_stream import apply_user_ws_message
from tyrex_pm.state.order_store import LocalOrder, OrderStore
from tyrex_pm.state.reconcile import reconcile_open_orders
from tyrex_pm.state.wallet_store import WalletStore


def test_provisional_local_within_grace_not_blocking_when_order_missing_from_venue_snapshot() -> None:
    w = WalletStore()
    o = OrderStore()
    ack = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    o.orders[ClientOrderId("c1")] = LocalOrder(
        client_order_id=ClientOrderId("c1"),
        venue_order_id=VenueOrderId("0xabc"),
        token_id=TokenId("t1"),
        side=Side.BUY,
        remaining=Decimal("10"),
        original_size=Decimal("10"),
        size_matched=Decimal("0"),
        confirmation="provisional",
        submit_ack_utc=ack,
        last_local_source="local",
    )
    res = reconcile_open_orders(
        w,
        o,
        venue_user_ws_stale=False,
        provisional_grace_s=90.0,
        now=datetime(2026, 1, 1, 12, 0, 30, tzinfo=timezone.utc),
    )
    assert res.blocking_drift_flags == ()
    assert "provisional_pending_venue" in res.drift_flags


def test_provisional_no_grace_when_user_ws_marked_stale() -> None:
    w = WalletStore()
    o = OrderStore()
    o.orders[ClientOrderId("c1")] = LocalOrder(
        client_order_id=ClientOrderId("c1"),
        venue_order_id=VenueOrderId("0xabc"),
        token_id=TokenId("t1"),
        side=Side.BUY,
        remaining=Decimal("10"),
        confirmation="provisional",
        submit_ack_utc=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        last_local_source="local",
    )
    res = reconcile_open_orders(
        w,
        o,
        venue_user_ws_stale=True,
        provisional_grace_s=90.0,
        now=datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc),
    )
    assert "local_open_not_on_venue" in res.blocking_drift_flags
    assert "provisional_pending_venue" not in res.drift_flags


def test_provisional_after_grace_blocks_when_still_missing_from_venue() -> None:
    w = WalletStore()
    o = OrderStore()
    ack = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    o.orders[ClientOrderId("c1")] = LocalOrder(
        client_order_id=ClientOrderId("c1"),
        venue_order_id=VenueOrderId("0xabc"),
        token_id=TokenId("t1"),
        side=Side.BUY,
        remaining=Decimal("10"),
        confirmation="provisional",
        submit_ack_utc=ack,
        last_local_source="local",
    )
    res = reconcile_open_orders(
        w,
        o,
        venue_user_ws_stale=False,
        provisional_grace_s=90.0,
        venue_confirm_provisional_timeout_s=400.0,
        now=ack.replace(hour=12, minute=3, second=0),
    )
    assert "local_open_not_on_venue" in res.blocking_drift_flags


def test_user_ws_placement_upgrades_provisional_local_order() -> None:
    w = WalletStore()
    o = OrderStore()
    cid = ClientOrderId("c1")
    o.orders[cid] = LocalOrder(
        client_order_id=cid,
        venue_order_id=VenueOrderId("0xorder1"),
        token_id=TokenId("123"),
        side=Side.BUY,
        remaining=Decimal("10"),
        confirmation="provisional",
        submit_ack_utc=datetime.now(timezone.utc),
        last_local_source="local",
    )
    apply_user_ws_message(
        w,
        {
            "type": "PLACEMENT",
            "id": "0xorder1",
            "asset_id": "123",
            "side": "BUY",
            "original_size": "10",
            "size_matched": "3",
            "price": "0.55",
            "status": "LIVE",
        },
        o,
    )
    lo = o.orders[cid]
    assert lo.confirmation == "venue_confirmed"
    assert lo.remaining == Decimal("7")
    assert lo.last_local_source == "user_ws"
    assert lo.size_matched == Decimal("3")


def test_user_ws_trade_matched_records_ledger_confirmed_moves_position() -> None:
    w = WalletStore()
    apply_user_ws_message(
        w,
        {
            "type": "TRADE",
            "asset_id": "999",
            "side": "BUY",
            "size": "2",
            "price": "0.5",
            "status": "MATCHED",
        },
    )
    assert len(w.trade_fill_records) == 1
    assert w.trade_fill_records[0].status == "MATCHED"
    assert not w.positions
    apply_user_ws_message(
        w,
        {
            "type": "TRADE",
            "asset_id": "999",
            "side": "BUY",
            "size": "2",
            "price": "0.5",
            "status": "CONFIRMED",
        },
    )
    assert len(w.trade_fill_records) == 2
    assert TokenId("999") in w.positions


def test_rest_does_not_clobber_user_ws_open_order_for_same_id() -> None:
    w = WalletStore()
    ws_view = OpenOrderView(
        token_id=TokenId("t"),
        side=Side.BUY,
        remaining_size=Decimal("5"),
        limit_price=Decimal("0.5"),
        client_order_id=None,
        venue_order_id=VenueOrderId("vid1"),
        original_size=Decimal("10"),
        size_matched=Decimal("5"),
        venue_state_source="user_ws",
    )
    rest_view = OpenOrderView(
        token_id=TokenId("t"),
        side=Side.BUY,
        remaining_size=Decimal("10"),
        limit_price=Decimal("0.5"),
        client_order_id=None,
        venue_order_id=VenueOrderId("vid1"),
        original_size=Decimal("10"),
        size_matched=Decimal("0"),
        venue_state_source="rest",
    )
    w._rest_open_orders = (rest_view,)
    w.user_ws_upsert_order(ws_view)
    assert len(w.open_orders) == 1
    assert w.open_orders[0].remaining_size == Decimal("5")
    assert w.open_orders[0].venue_state_source == "user_ws"


def test_ws_cancellation_tombstone_hides_stale_rest_row() -> None:
    w = WalletStore()
    rest_view = OpenOrderView(
        token_id=TokenId("t"),
        side=Side.BUY,
        remaining_size=Decimal("10"),
        limit_price=Decimal("0.5"),
        client_order_id=None,
        venue_order_id=VenueOrderId("vid1"),
        original_size=Decimal("10"),
        size_matched=Decimal("0"),
        venue_state_source="rest",
    )
    w._rest_open_orders = (rest_view,)
    w.user_ws_upsert_order(
        OpenOrderView(
            token_id=TokenId("t"),
            side=Side.BUY,
            remaining_size=Decimal("10"),
            limit_price=Decimal("0.5"),
            client_order_id=None,
            venue_order_id=VenueOrderId("vid1"),
            original_size=Decimal("10"),
            size_matched=Decimal("0"),
            venue_state_source="user_ws",
        )
    )
    apply_user_ws_message(w, {"type": "CANCELLATION", "id": "vid1"})
    assert w.open_orders == ()


def test_positions_not_updated_from_open_order_events() -> None:
    w = WalletStore()
    apply_user_ws_message(
        w,
        {
            "type": "PLACEMENT",
            "id": "0xx",
            "asset_id": "888",
            "side": "BUY",
            "original_size": "100",
            "size_matched": "0",
            "price": "0.4",
        },
    )
    assert not w.positions


def test_balance_untouched_by_user_ws_order_and_trade_matched() -> None:
    w = WalletStore()
    w.usdc_balance = Decimal("1000")
    apply_user_ws_message(
        w,
        {
            "type": "TRADE",
            "asset_id": "888",
            "side": "BUY",
            "size": "1",
            "price": "0.5",
            "status": "MATCHED",
        },
    )
    assert w.usdc_balance == Decimal("1000")


def test_user_ws_cancellation_removes_local_oms_by_venue_order_id() -> None:
    w = WalletStore()
    o = OrderStore()
    cid = ClientOrderId("c1")
    vid = VenueOrderId("0xv1")
    o.orders[cid] = LocalOrder(
        client_order_id=cid,
        venue_order_id=vid,
        token_id=TokenId("t"),
        side=Side.BUY,
        remaining=Decimal("3"),
        confirmation="venue_confirmed",
        last_local_source="user_ws",
    )
    w.user_ws_upsert_order(
        OpenOrderView(
            token_id=TokenId("t"),
            side=Side.BUY,
            remaining_size=Decimal("3"),
            limit_price=Decimal("0.5"),
            client_order_id=None,
            venue_order_id=vid,
            venue_state_source="user_ws",
        )
    )
    apply_user_ws_message(w, {"type": "CANCELLATION", "id": "0xv1"}, o)
    assert cid not in o.orders
    res = reconcile_open_orders(w, o)
    assert res.drift_flags == ()


def test_user_ws_update_full_fill_removes_local_oms() -> None:
    w = WalletStore()
    o = OrderStore()
    cid = ClientOrderId("c1")
    o.orders[cid] = LocalOrder(
        client_order_id=cid,
        venue_order_id=VenueOrderId("0xfull"),
        token_id=TokenId("99"),
        side=Side.BUY,
        remaining=Decimal("2"),
        confirmation="venue_confirmed",
        last_local_source="user_ws",
    )
    apply_user_ws_message(
        w,
        {
            "type": "UPDATE",
            "id": "0xfull",
            "asset_id": "99",
            "side": "BUY",
            "original_size": "10",
            "size_matched": "10",
            "price": "0.5",
        },
        o,
    )
    assert cid not in o.orders
    assert w.open_orders == ()


def test_reconcile_prunes_venue_confirmed_when_absent_from_merged_book_ws_fresh() -> None:
    w = WalletStore()
    o = OrderStore()
    cid = ClientOrderId("c1")
    o.orders[cid] = LocalOrder(
        client_order_id=cid,
        venue_order_id=VenueOrderId("gone-ui"),
        token_id=TokenId("t"),
        side=Side.BUY,
        remaining=Decimal("1"),
        confirmation="venue_confirmed",
        last_local_source="user_ws",
    )
    res = reconcile_open_orders(w, o, venue_user_ws_stale=False)
    assert res.drift_flags == ()
    assert res.blocking_drift_flags == ()
    assert "gone-ui" in res.pruned_terminal_venue_order_ids
    assert cid not in o.orders


def test_reconcile_does_not_prune_venue_confirmed_when_ws_stale_blocks_until_repaired() -> None:
    w = WalletStore()
    o = OrderStore()
    cid = ClientOrderId("c1")
    o.orders[cid] = LocalOrder(
        client_order_id=cid,
        venue_order_id=VenueOrderId("gone-ui"),
        token_id=TokenId("t"),
        side=Side.BUY,
        remaining=Decimal("1"),
        confirmation="venue_confirmed",
        last_local_source="user_ws",
    )
    res = reconcile_open_orders(w, o, venue_user_ws_stale=True)
    assert "local_open_not_on_venue" in res.blocking_drift_flags
    assert res.pruned_terminal_venue_order_ids == ()
    assert cid in o.orders


def test_persistent_size_mismatch_still_blocking() -> None:
    w = WalletStore()
    w.open_orders = (
        OpenOrderView(
            token_id=TokenId("t"),
            side=Side.BUY,
            remaining_size=Decimal("9"),
            limit_price=Decimal("0.5"),
            client_order_id=None,
            venue_order_id=VenueOrderId("v1"),
            original_size=Decimal("10"),
            size_matched=Decimal("1"),
            venue_state_source="user_ws",
        ),
    )
    o = OrderStore()
    o.orders[ClientOrderId("x")] = LocalOrder(
        client_order_id=ClientOrderId("x"),
        venue_order_id=VenueOrderId("v1"),
        token_id=TokenId("t"),
        side=Side.BUY,
        remaining=Decimal("5"),
        confirmation="venue_confirmed",
        last_local_source="user_ws",
    )
    res = reconcile_open_orders(w, o)
    assert "open_order_size_mismatch" in res.blocking_drift_flags
