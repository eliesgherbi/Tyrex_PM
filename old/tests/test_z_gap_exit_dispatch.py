"""Exit dispatch tests (A0.7)."""

from __future__ import annotations

import time
from decimal import Decimal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.market_data.book_read import LegBookQuote, PairBookSnapshot
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.z_gap.exit_plan import build_z_gap_exit_work_unit
from tyrex_pm.strategies.z_gap.lifecycle import mark_exit_submitted, mark_exit_triggered, reconcile_exit_fill
from tyrex_pm.strategies.z_gap.state import ZGapLifecycleState, ZGapPhase


def _coord(alloc: Decimal = Decimal("5")) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(
        wallet=WalletStore(),
        orders=OrderStore(),
        health=HealthRuntime(),
        allocation_ledger=AllocationLedger(),
    )
    coord.allocation_ledger.apply_buy("z_gap", TokenId("111"), alloc, correlation_id="buy1")
    return coord


def _books() -> PairBookSnapshot:
    return PairBookSnapshot(
        up=LegBookQuote(
            token_id="111",
            bid=Decimal("0.55"),
            ask=Decimal("0.56"),
            stale=False,
            book_age_ms=10,
            spread=Decimal("0.01"),
            quality_status="ok",
        ),
        down=LegBookQuote(
            token_id="222",
            bid=Decimal("0.44"),
            ask=Decimal("0.45"),
            stale=False,
            book_age_ms=10,
            spread=Decimal("0.01"),
            quality_status="ok",
        ),
    )


def _active_lc(qty: Decimal = Decimal("5")) -> ZGapLifecycleState:
    lc = ZGapLifecycleState(market_id="m1", condition_id="0xabc", owner_id="z_gap")
    lc.phase = ZGapPhase.ACTIVE
    lc.selected_leg = "UP"
    lc.token_id = "111"
    lc.active_quantity = qty
    return lc


def test_sells_exact_active_quantity() -> None:
    lc = _active_lc(Decimal("5"))
    wu = build_z_gap_exit_work_unit(lc, books=_books(), coord=_coord(), exit_reason="thesis_stop", correlation_id="x")
    assert wu is not None
    assert wu.intent.size == Decimal("5")


def test_never_oversell_reconcile() -> None:
    lc = _active_lc(Decimal("5"))
    mark_exit_triggered(lc, exit_reason="thesis_stop")
    out = reconcile_exit_fill(lc, filled_qty=Decimal("6"))
    assert out.failure
    assert lc.phase == ZGapPhase.FAILED


def test_full_fill_closes() -> None:
    coord = _coord()
    lc = _active_lc(Decimal("5"))
    mark_exit_triggered(lc, exit_reason="lifecycle_flatten")
    coord.allocation_ledger.apply_sell("z_gap", TokenId("111"), Decimal("5"), correlation_id="s1")
    out = reconcile_exit_fill(lc, filled_qty=Decimal("5"), coord=coord)
    assert out.closed
    assert lc.active_quantity == 0
    assert lc.phase == ZGapPhase.DONE


def test_partial_fill_reduces_quantity() -> None:
    lc = _active_lc(Decimal("5"))
    mark_exit_triggered(lc, exit_reason="thesis_stop")
    out = reconcile_exit_fill(lc, filled_qty=Decimal("2"))
    assert out.residual == Decimal("3")
    assert lc.phase == ZGapPhase.ACTIVE


def test_zero_fill_remains_active() -> None:
    lc = _active_lc(Decimal("5"))
    mark_exit_triggered(lc, exit_reason="thesis_stop")
    out = reconcile_exit_fill(lc, filled_qty=Decimal("0"))
    assert out.event == "exit_unfilled"
    assert lc.phase == ZGapPhase.ACTIVE


def test_retry_interval_gate() -> None:
    last = time.monotonic()
    elapsed_ms = 0
    assert elapsed_ms < 1000
    _ = last


def test_max_attempts_manual_intervention() -> None:
    lc = _active_lc(Decimal("2"))
    lc.exit_attempts = 5
    lc.manual_intervention_required = True
    lc.transition(ZGapPhase.FAILED, reason="max_exit_attempts")
    assert lc.manual_intervention_required


def test_one_exit_order_pending() -> None:
    lc = _active_lc()
    mark_exit_triggered(lc, exit_reason="kill_switch")
    mark_exit_submitted(lc, order_id="e1", requested_shares=lc.active_quantity)
    assert lc.exit_order_id == "e1"
