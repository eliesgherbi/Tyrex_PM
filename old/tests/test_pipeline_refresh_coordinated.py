from __future__ import annotations

from decimal import Decimal

import pytest

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import ClientOrderId, TokenId, VenueOrderId
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.pipeline import _reconcile_kw, refresh_wallet_coordinated_after_live_submit
from tyrex_pm.state.order_store import LocalOrder, OrderStore
from tyrex_pm.state.reconcile import reconcile_open_orders
from tyrex_pm.state.wallet_store import WalletStore


class _FlakyOrdersClob:
    """First REST snapshot empty; second includes the resting order (simulates post-submit lag).

    Note on V2 method naming
    ------------------------
    The V2 SDK exposes ``get_open_orders()`` (V1 was ``get_orders()``); the
    counter is named ``get_orders_calls`` for historical readability only —
    it counts ``get_open_orders`` invocations now.
    """

    def __init__(self) -> None:
        self.get_orders_calls = 0

    def get_balance_allowance(self, params: object) -> dict:
        return {"balance": "1000", "allowance": "1000000"}

    def get_open_orders(self) -> list[dict]:
        self.get_orders_calls += 1
        if self.get_orders_calls == 1:
            return []
        return [
            {
                "id": "v1",
                "asset_id": "tok99",
                "side": "BUY",
                "original_size": "8",
                "size_matched": "0",
                "price": "0.5",
            }
        ]


@pytest.mark.asyncio
async def test_coordinated_refresh_second_snapshot_clears_transient_local_open_miss() -> None:
    wallet = WalletStore()
    orders = OrderStore()
    cid = ClientOrderId("c1")
    orders.orders[cid] = LocalOrder(
        client_order_id=cid,
        venue_order_id=VenueOrderId("v1"),
        token_id=TokenId("tok99"),
        side=Side.BUY,
        remaining=Decimal("8"),
    )
    coord = RuntimeCoordinator(wallet=wallet, orders=orders, health=HealthRuntime())
    clob = _FlakyOrdersClob()
    await refresh_wallet_coordinated_after_live_submit(coord, clob, transient_retry_s=0.01)
    res = reconcile_open_orders(coord.wallet, coord.orders, **_reconcile_kw(coord))
    assert res.drift_flags == ()
    assert clob.get_orders_calls == 2


@pytest.mark.asyncio
async def test_coordinated_refresh_aligns_local_remaining_from_venue_one_fetch() -> None:
    """Stale local size is corrected from venue truth (no spurious size drift, no extra REST round trip)."""
    wallet = WalletStore()
    orders = OrderStore()
    cid = ClientOrderId("c1")
    orders.orders[cid] = LocalOrder(
        client_order_id=cid,
        venue_order_id=VenueOrderId("v1"),
        token_id=TokenId("tok99"),
        side=Side.BUY,
        remaining=Decimal("8"),
    )

    class _Clob:
        def __init__(self) -> None:
            self.get_orders_calls = 0

        def get_balance_allowance(self, params: object) -> dict:
            return {"balance": "1000", "allowance": "1000000"}

        def get_open_orders(self) -> list[dict]:
            self.get_orders_calls += 1
            return [
                {
                    "id": "v1",
                    "asset_id": "tok99",
                    "side": "BUY",
                    "original_size": "10",
                    "size_matched": "0",
                    "price": "0.5",
                }
            ]

    coord = RuntimeCoordinator(wallet=wallet, orders=orders, health=HealthRuntime())
    clob = _Clob()
    await refresh_wallet_coordinated_after_live_submit(coord, clob, transient_retry_s=0.01)
    res = reconcile_open_orders(coord.wallet, coord.orders, **_reconcile_kw(coord))
    assert res.drift_flags == ()
    assert orders.orders[cid].remaining == Decimal("10")
    assert clob.get_orders_calls == 1


@pytest.mark.asyncio
async def test_coordinated_refresh_does_not_retry_when_venue_has_extra_untracked_order() -> None:
    """Only an all-`local_open_not_on_venue` drift schedule triggers the lag retry."""

    class _Clob:
        def __init__(self) -> None:
            self.get_orders_calls = 0

        def get_balance_allowance(self, params: object) -> dict:
            return {"balance": "1000", "allowance": "1000000"}

        def get_open_orders(self) -> list[dict]:
            self.get_orders_calls += 1
            return [
                {
                    "id": "v1",
                    "asset_id": "tok99",
                    "side": "BUY",
                    "original_size": "8",
                    "size_matched": "0",
                    "price": "0.5",
                },
                {
                    "id": "v2_stray",
                    "asset_id": "tok88",
                    "side": "BUY",
                    "original_size": "1",
                    "size_matched": "0",
                    "price": "0.5",
                },
            ]

    wallet = WalletStore()
    orders = OrderStore()
    cid = ClientOrderId("c1")
    orders.orders[cid] = LocalOrder(
        client_order_id=cid,
        venue_order_id=VenueOrderId("v1"),
        token_id=TokenId("tok99"),
        side=Side.BUY,
        remaining=Decimal("8"),
    )
    coord = RuntimeCoordinator(wallet=wallet, orders=orders, health=HealthRuntime())
    clob = _Clob()
    await refresh_wallet_coordinated_after_live_submit(coord, clob, transient_retry_s=0.01)
    res = reconcile_open_orders(coord.wallet, coord.orders, **_reconcile_kw(coord))
    assert "venue_open_not_tracked_locally" in res.drift_flags
    assert clob.get_orders_calls == 1
