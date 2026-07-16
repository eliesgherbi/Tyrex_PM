"""Reconciliation tests for Z-Gap enforce (A0.8)."""

from __future__ import annotations

import time
from decimal import Decimal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.runtime.config import ZGapReconciliationConfig
from tyrex_pm.core.models import WalletPosition
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.z_gap.reconciliation import (
    RECON_CONSISTENT,
    RECON_MISMATCH,
    RECON_UNRESOLVED,
    RECON_VENUE_LAG_EXPECTED,
    ReconciliationTracker,
    duplicate_fill_idempotent,
    evaluate_position_reconciliation,
)
from tyrex_pm.strategies.z_gap.state import ZGapLifecycleState, ZGapPhase

CFG = ZGapReconciliationConfig(venue_sync_grace_ms=100, poll_interval_ms=50, max_attempts=3)


def _coord() -> RuntimeCoordinator:
    return RuntimeCoordinator(
        wallet=WalletStore(),
        orders=OrderStore(),
        health=HealthRuntime(),
        allocation_ledger=AllocationLedger(),
    )


def _lc(qty: Decimal = Decimal("5")) -> ZGapLifecycleState:
    lc = ZGapLifecycleState(market_id="m1", condition_id="0x", owner_id="z_gap")
    lc.phase = ZGapPhase.ACTIVE
    lc.token_id = "111"
    lc.active_quantity = qty
    return lc


def test_confirmed_fill_before_wallet_update_venue_lag() -> None:
    coord = _coord()
    lc = _lc(Decimal("5"))
    coord.allocation_ledger.apply_buy("z_gap", TokenId("111"), Decimal("5"), correlation_id="b1")
    tracker = ReconciliationTracker()
    t0 = time.monotonic()
    r1 = evaluate_position_reconciliation(coord, lc, cfg=CFG, tracker=tracker, now_mono=t0)
    assert r1.status == RECON_VENUE_LAG_EXPECTED
    coord.wallet.positions[TokenId("111")] = WalletPosition(token_id=TokenId("111"), qty=Decimal("5"))
    r2 = evaluate_position_reconciliation(coord, lc, cfg=CFG, tracker=tracker, now_mono=t0 + 0.05)
    assert r2.status == RECON_CONSISTENT


def test_wallet_never_catches_up_fails_closed() -> None:
    coord = _coord()
    lc = _lc(Decimal("5"))
    coord.allocation_ledger.apply_buy("z_gap", TokenId("111"), Decimal("5"), correlation_id="b1")
    tracker = ReconciliationTracker()
    t0 = time.monotonic()
    evaluate_position_reconciliation(coord, lc, cfg=CFG, tracker=tracker, now_mono=t0)
    r = evaluate_position_reconciliation(coord, lc, cfg=CFG, tracker=tracker, now_mono=t0 + 0.5)
    assert r.status == RECON_UNRESOLVED
    assert r.fail_closed


def test_allocation_mismatch_fails_closed() -> None:
    coord = _coord()
    lc = _lc(Decimal("5"))
    coord.allocation_ledger.apply_buy("z_gap", TokenId("111"), Decimal("3"), correlation_id="b1")
    r = evaluate_position_reconciliation(coord, lc, cfg=CFG, tracker=ReconciliationTracker(), now_mono=time.monotonic())
    assert r.status == RECON_MISMATCH


def test_duplicate_fill_idempotent() -> None:
    coord = _coord()
    before = coord.allocation_ledger.get_allocated("z_gap", TokenId("111"))
    coord.allocation_ledger.apply_buy("z_gap", TokenId("111"), Decimal("5"), correlation_id="b1")
    assert duplicate_fill_idempotent(coord, owner_id="z_gap", token_id=TokenId("111"), qty=Decimal("5"), before_qty=before)
