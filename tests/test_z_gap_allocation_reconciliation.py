"""Allocation reconciliation tests (A0.7)."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.z_gap.lifecycle import reconcile_entry_fill, reconcile_exit_fill, verify_allocation_reconciled
from tyrex_pm.state.entry_fill_lifecycle import EntryFillStatus, OrderFillSnapshot
from tyrex_pm.strategies.z_gap.lifecycle import mark_entry_submitted, mark_exit_triggered
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


def test_allocation_added_on_buy_fill() -> None:
    coord = _coord()
    lc = _lc()
    mark_entry_submitted(
        lc,
        order_id="c1",
        requested_shares=Decimal("5"),
        selected_leg="UP",
        token_id="111",
        model_p=None,
        entry_z=None,
        entry_edge=None,
    )
    coord.allocation_ledger.apply_buy("z_gap", TokenId("111"), Decimal("5"), correlation_id="b1")
    snap = OrderFillSnapshot(
        token_id=TokenId("111"),
        owner_id="z_gap",
        client_order_id="c1",
        submitted_qty=Decimal("5"),
        filled_qty=Decimal("5"),
        confirmed_qty=Decimal("5"),
        remaining_qty=Decimal("0"),
        status=EntryFillStatus.MATCHED,
        source="oms",
        sellable_qty=Decimal("5"),
    )
    reconcile_entry_fill(lc, snap, coord=coord)
    ok, _, alloc = verify_allocation_reconciled(coord, lc)
    assert ok
    assert alloc == Decimal("5")


def test_allocation_reduced_on_sell_fill() -> None:
    coord = _coord()
    lc = _lc()
    lc.phase = ZGapPhase.ACTIVE
    lc.token_id = "111"
    lc.active_quantity = Decimal("5")
    coord.allocation_ledger.apply_buy("z_gap", TokenId("111"), Decimal("5"), correlation_id="b1")
    mark_exit_triggered(lc, exit_reason="thesis_stop")
    coord.allocation_ledger.apply_sell("z_gap", TokenId("111"), Decimal("5"), correlation_id="s1")
    reconcile_exit_fill(lc, filled_qty=Decimal("5"), coord=coord)
    assert coord.allocation_ledger.get_allocated("z_gap", TokenId("111")) == Decimal("0")


def test_final_allocation_zero_after_close() -> None:
    coord = _coord()
    lc = _lc()
    lc.phase = ZGapPhase.ACTIVE
    lc.token_id = "111"
    lc.active_quantity = Decimal("3")
    coord.allocation_ledger.apply_buy("z_gap", TokenId("111"), Decimal("3"), correlation_id="b1")
    mark_exit_triggered(lc, exit_reason="lifecycle_flatten")
    coord.allocation_ledger.apply_sell("z_gap", TokenId("111"), Decimal("3"), correlation_id="s1")
    out = reconcile_exit_fill(lc, filled_qty=Decimal("3"), coord=coord)
    assert out.closed
    assert coord.allocation_ledger.get_allocated("z_gap", TokenId("111")) == 0


def test_reconciliation_mismatch_fails_closed() -> None:
    coord = _coord()
    lc = _lc()
    mark_entry_submitted(
        lc,
        order_id="c1",
        requested_shares=Decimal("5"),
        selected_leg="UP",
        token_id="111",
        model_p=None,
        entry_z=None,
        entry_edge=None,
    )
    coord.allocation_ledger.apply_buy("z_gap", TokenId("111"), Decimal("3"), correlation_id="b1")
    snap = OrderFillSnapshot(
        token_id=TokenId("111"),
        owner_id="z_gap",
        client_order_id="c1",
        submitted_qty=Decimal("5"),
        filled_qty=Decimal("5"),
        confirmed_qty=Decimal("5"),
        remaining_qty=Decimal("0"),
        status=EntryFillStatus.MATCHED,
        source="oms",
        sellable_qty=Decimal("5"),
    )
    out = reconcile_entry_fill(lc, snap, coord=coord)
    assert out.failure
    assert lc.phase == ZGapPhase.FAILED
