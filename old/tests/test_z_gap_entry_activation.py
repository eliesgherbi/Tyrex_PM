"""Entry activation and fill reconciliation tests (A0.7)."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.entry_fill_lifecycle import EntryFillStatus, OrderFillSnapshot
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.z_gap.lifecycle import mark_entry_submitted, reconcile_entry_fill
from tyrex_pm.strategies.z_gap.state import ZGapLifecycleState, ZGapPhase


def _coord() -> RuntimeCoordinator:
    return RuntimeCoordinator(
        wallet=WalletStore(),
        orders=OrderStore(),
        health=HealthRuntime(),
        allocation_ledger=AllocationLedger(),
    )


def _lc() -> ZGapLifecycleState:
    return ZGapLifecycleState(market_id="m1", condition_id="0xabc", owner_id="z_gap")


def test_allocation_equals_confirmed_fill() -> None:
    coord = _coord()
    lc = _lc()
    mark_entry_submitted(
        lc,
        order_id="c1",
        requested_shares=Decimal("8"),
        selected_leg="UP",
        token_id="111",
        model_p=Decimal("0.6"),
        entry_z=Decimal("1.1"),
        entry_edge=Decimal("0.04"),
    )
    coord.allocation_ledger.apply_buy(
        owner_id="z_gap",
        token_id=TokenId("111"),
        qty=Decimal("8"),
        correlation_id="x",
    )
    snap = OrderFillSnapshot(
        token_id=TokenId("111"),
        owner_id="z_gap",
        client_order_id="c1",
        submitted_qty=Decimal("8"),
        filled_qty=Decimal("8"),
        confirmed_qty=Decimal("8"),
        remaining_qty=Decimal("0"),
        status=EntryFillStatus.MATCHED,
        source="oms",
        sellable_qty=Decimal("8"),
    )
    out = reconcile_entry_fill(lc, snap, coord=coord)
    assert out.activated
    assert lc.active_quantity == Decimal("8")
    assert coord.allocation_ledger.get_allocated("z_gap", TokenId("111")) == Decimal("8")


def test_requested_not_treated_as_filled() -> None:
    lc = _lc()
    mark_entry_submitted(
        lc,
        order_id="c1",
        requested_shares=Decimal("8"),
        selected_leg="UP",
        token_id="111",
        model_p=None,
        entry_z=None,
        entry_edge=None,
    )
    snap = OrderFillSnapshot(
        token_id=TokenId("111"),
        owner_id="z_gap",
        client_order_id="c1",
        submitted_qty=Decimal("8"),
        filled_qty=Decimal("0"),
        confirmed_qty=Decimal("0"),
        remaining_qty=Decimal("8"),
        status=EntryFillStatus.SUBMITTED,
        source="oms",
        sellable_qty=Decimal("0"),
    )
    out = reconcile_entry_fill(lc, snap)
    assert not out.activated
    assert lc.phase == ZGapPhase.ENTRY_PENDING


def test_zero_fill_done() -> None:
    lc = _lc()
    mark_entry_submitted(
        lc,
        order_id="c1",
        requested_shares=Decimal("8"),
        selected_leg="UP",
        token_id="111",
        model_p=None,
        entry_z=None,
        entry_edge=None,
    )
    snap = OrderFillSnapshot(
        token_id=TokenId("111"),
        owner_id="z_gap",
        client_order_id="c1",
        submitted_qty=Decimal("8"),
        filled_qty=Decimal("0"),
        confirmed_qty=Decimal("0"),
        remaining_qty=Decimal("0"),
        status=EntryFillStatus.EXPIRED,
        source="oms",
        sellable_qty=Decimal("0"),
    )
    out = reconcile_entry_fill(lc, snap)
    assert out.event == "entry_unfilled"
    assert lc.phase == ZGapPhase.DONE


def test_entry_model_snapshot_persists() -> None:
    lc = _lc()
    mark_entry_submitted(
        lc,
        order_id="c1",
        requested_shares=Decimal("5"),
        selected_leg="DOWN",
        token_id="222",
        model_p=Decimal("0.42"),
        entry_z=Decimal("-1.2"),
        entry_edge=Decimal("0.05"),
    )
    assert lc.entry_model_p == Decimal("0.42")
    assert lc.entry_z == Decimal("-1.2")
