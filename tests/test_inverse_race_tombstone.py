"""Tests for the inverse-race tombstone (Issue A).

Race we are guarding against:

    1. WS UPDATE arrives for vid X with ``remaining_size <= 0`` (full fill / matched-out).
    2. Local OMS row for X is removed (existing behaviour, kept).
    3. WS map entry for X is removed (existing behaviour, kept).
    4. NEW: a tombstone is stamped for X so that a stale REST snapshot returned moments later
       does not resurrect X in ``wallet.open_orders``.

Without the tombstone, reconcile briefly sees X as ``venue_open_not_tracked_locally`` because
local has nothing while merged-REST still shows X. With it, the merged view stays clean
until REST catches up (or until the TTL expires and REST is allowed to re-hydrate).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import ClientOrderId, TokenId, VenueOrderId
from tyrex_pm.core.models import OpenOrderView
from tyrex_pm.core.time import utc_now
from tyrex_pm.ingestion.user_stream import apply_user_ws_message
from tyrex_pm.state.order_store import LocalOrder, OrderStore
from tyrex_pm.state.reconcile import reconcile_open_orders
from tyrex_pm.state.wallet_store import WalletStore


def _ws_terminal_msg(vid: str, asset: str = "tok-A", original: str = "10", matched: str = "10") -> dict:
    return {
        "type": "UPDATE",
        "id": vid,
        "asset_id": asset,
        "side": "BUY",
        "original_size": original,
        "size_matched": matched,
        "price": "0.5",
        "status": "MATCHED",
    }


def _rest_view(vid: str, asset: str = "tok-A", remaining: str = "10") -> OpenOrderView:
    return OpenOrderView(
        token_id=TokenId(asset),
        side=Side.BUY,
        remaining_size=Decimal(remaining),
        limit_price=Decimal("0.5"),
        client_order_id=None,
        venue_order_id=VenueOrderId(vid),
        original_size=Decimal("10"),
        size_matched=Decimal(str(Decimal("10") - Decimal(remaining))),
        venue_state_source="rest",
    )


# ---------------------------------------------------------------------------
# Case 1: WS terminal update with rem<=0 removes local OMS row AND stamps tombstone.
# ---------------------------------------------------------------------------
def test_ws_terminal_update_removes_local_row_and_stamps_tombstone() -> None:
    w = WalletStore()
    o = OrderStore()
    cid = ClientOrderId("c1")
    o.orders[cid] = LocalOrder(
        client_order_id=cid,
        venue_order_id=VenueOrderId("0xfill"),
        token_id=TokenId("tok-A"),
        side=Side.BUY,
        remaining=Decimal("4"),
        confirmation="venue_confirmed",
        last_local_source="user_ws",
    )

    apply_user_ws_message(w, _ws_terminal_msg("0xfill"), o)

    assert cid not in o.orders, "local row must be cleaned up by WS terminal"
    assert "0xfill" in w._ws_cancel_tombstones, (
        "WS-terminal upsert (rem<=0) must stamp a suppression tombstone, otherwise a stale "
        "REST snapshot can resurrect this id and trigger venue_open_not_tracked_locally"
    )
    assert "0xfill" not in w._user_ws_order_map


# ---------------------------------------------------------------------------
# Case 2: stale REST snapshot for the same vid is suppressed while tombstone is active,
# and reconcile produces no false drift.
# ---------------------------------------------------------------------------
def test_stale_rest_snapshot_suppressed_no_false_drift_while_tombstone_active() -> None:
    w = WalletStore()
    o = OrderStore()
    cid = ClientOrderId("c1")
    o.orders[cid] = LocalOrder(
        client_order_id=cid,
        venue_order_id=VenueOrderId("0xfill"),
        token_id=TokenId("tok-A"),
        side=Side.BUY,
        remaining=Decimal("4"),
        confirmation="venue_confirmed",
        last_local_source="user_ws",
    )

    apply_user_ws_message(w, _ws_terminal_msg("0xfill"), o)

    w._rest_open_orders = (_rest_view("0xfill", remaining="4"),)
    w.rebuild_open_orders_merged()

    assert w.open_orders == (), (
        "merged view must hide the REST resurrection while the tombstone is active"
    )
    assert w.get_tombstoned_rest_vids() == ("0xfill",), (
        "tombstoned_rest_vids must surface the suppression for operator observability"
    )

    res = reconcile_open_orders(w, o, venue_user_ws_stale=False)
    assert res.drift_flags == ()
    assert res.blocking_drift_flags == ()


# ---------------------------------------------------------------------------
# Case 3: after tombstone TTL expiry, REST is allowed to re-hydrate normally.
# ---------------------------------------------------------------------------
def test_after_tombstone_expiry_rest_rehydrates_normally() -> None:
    from tyrex_pm.state import wallet_store as ws_mod

    w = WalletStore()
    apply_user_ws_message(w, _ws_terminal_msg("0xfill"))
    assert "0xfill" in w._ws_cancel_tombstones

    expired = utc_now() - timedelta(seconds=ws_mod._WS_CANCEL_TOMBSTONE_TTL_S + 60)
    w._ws_cancel_tombstones["0xfill"] = expired

    w._rest_open_orders = (_rest_view("0xfill", remaining="10"),)
    w.rebuild_open_orders_merged()

    assert len(w.open_orders) == 1
    assert str(w.open_orders[0].venue_order_id) == "0xfill"
    assert w.open_orders[0].venue_state_source == "rest"
    assert w.get_tombstoned_rest_vids() == ()


# ---------------------------------------------------------------------------
# Case 4: real unmatched venue order with no tombstone still produces blocking drift.
# (regression guard: we did not weaken genuine drift detection)
# ---------------------------------------------------------------------------
def test_real_unmatched_venue_order_without_tombstone_still_blocks() -> None:
    w = WalletStore()
    o = OrderStore()
    w._rest_open_orders = (_rest_view("0xforeign", remaining="10"),)
    w.rebuild_open_orders_merged()

    assert w.get_tombstoned_rest_vids() == ()
    assert len(w.open_orders) == 1

    res = reconcile_open_orders(w, o, venue_user_ws_stale=False)
    assert "venue_open_not_tracked_locally" in res.blocking_drift_flags


# ---------------------------------------------------------------------------
# Case 5: existing WS CANCELLATION tombstone behaviour is unchanged.
# ---------------------------------------------------------------------------
def test_ws_cancellation_path_still_tombstones_and_hides_rest_unchanged() -> None:
    w = WalletStore()
    w.user_ws_upsert_order(
        OpenOrderView(
            token_id=TokenId("tok-A"),
            side=Side.BUY,
            remaining_size=Decimal("10"),
            limit_price=Decimal("0.5"),
            client_order_id=None,
            venue_order_id=VenueOrderId("0xcanc"),
            original_size=Decimal("10"),
            size_matched=Decimal("0"),
            venue_state_source="user_ws",
        )
    )
    assert "0xcanc" not in w._ws_cancel_tombstones, "live upsert must clear any prior tombstone"

    apply_user_ws_message(w, {"type": "CANCELLATION", "id": "0xcanc"})

    assert "0xcanc" in w._ws_cancel_tombstones
    w._rest_open_orders = (_rest_view("0xcanc", remaining="10"),)
    w.rebuild_open_orders_merged()
    assert w.open_orders == ()
    assert w.get_tombstoned_rest_vids() == ("0xcanc",)


# ---------------------------------------------------------------------------
# Case 6 (extra): a subsequent live WS upsert (rem>0) clears the terminal tombstone.
# Defensive guard against any platform corner case where an id is reported terminal
# and then live again.
# ---------------------------------------------------------------------------
def test_live_ws_upsert_after_terminal_clears_tombstone() -> None:
    w = WalletStore()
    apply_user_ws_message(w, _ws_terminal_msg("0xrev"))
    assert "0xrev" in w._ws_cancel_tombstones

    w.user_ws_upsert_order(
        OpenOrderView(
            token_id=TokenId("tok-A"),
            side=Side.BUY,
            remaining_size=Decimal("3"),
            limit_price=Decimal("0.5"),
            client_order_id=None,
            venue_order_id=VenueOrderId("0xrev"),
            original_size=Decimal("10"),
            size_matched=Decimal("7"),
            venue_state_source="user_ws",
        )
    )
    assert "0xrev" not in w._ws_cancel_tombstones
    assert "0xrev" in w._user_ws_order_map
