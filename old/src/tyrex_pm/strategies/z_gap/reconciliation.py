"""Position reconciliation for Z-Gap enforce runtime (A0.8).

Source-of-truth hierarchy during active runtime:
1. Confirmed OMS/fill events (lifecycle active_quantity)
2. Allocation ledger
3. Venue/wallet position snapshot (may lag)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from tyrex_pm.core.ids import TokenId
from tyrex_pm.risk.inventory import available_to_sell
from tyrex_pm.runtime.config import ZGapReconciliationConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.strategies.z_gap.lifecycle import allocated_quantity
from tyrex_pm.strategies.z_gap.state import ZGapLifecycleState

RECON_CONSISTENT = "consistent"
RECON_VENUE_LAG_EXPECTED = "venue_lag_expected"
RECON_MISMATCH = "mismatch"
RECON_UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class PositionReconciliationResult:
    status: str
    lifecycle_qty: Decimal
    allocated_qty: Decimal
    venue_qty: Decimal
    sellable_qty: Decimal
    grace_elapsed_ms: float | None
    reason: str | None = None
    fail_closed: bool = False

    def to_fact_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "lifecycle_qty": str(self.lifecycle_qty),
            "allocated_qty": str(self.allocated_qty),
            "venue_qty": str(self.venue_qty),
            "sellable_qty": str(self.sellable_qty),
            "grace_elapsed_ms": self.grace_elapsed_ms,
            "reason": self.reason,
            "fail_closed": self.fail_closed,
        }


def venue_reported_quantity(coord: RuntimeCoordinator, token_id: str | TokenId) -> Decimal:
    tid = TokenId(str(token_id))
    pos = coord.wallet.positions.get(tid)
    if pos is None or pos.qty <= 0:
        return Decimal("0")
    return pos.qty


def sellable_quantity(coord: RuntimeCoordinator, token_id: str | TokenId) -> Decimal:
    tid = TokenId(str(token_id))
    positions = {p.token_id: p for p in coord.wallet.positions.values()}
    avail = available_to_sell(
        token_id=tid,
        positions=positions,
        in_flight=coord.orders.in_flight_by_token,
    )
    return avail


@dataclass
class ReconciliationTracker:
    """Tracks first observation of venue lag for grace-period evaluation."""

    first_lag_mono: float | None = None

    def reset(self) -> None:
        self.first_lag_mono = None


def evaluate_position_reconciliation(
    coord: RuntimeCoordinator,
    lifecycle: ZGapLifecycleState,
    *,
    cfg: ZGapReconciliationConfig,
    tracker: ReconciliationTracker,
    now_mono: float,
) -> PositionReconciliationResult:
    lifecycle_qty = lifecycle.active_quantity
    if lifecycle.token_id is None:
        return PositionReconciliationResult(
            status=RECON_CONSISTENT,
            lifecycle_qty=lifecycle_qty,
            allocated_qty=Decimal("0"),
            venue_qty=Decimal("0"),
            sellable_qty=Decimal("0"),
            grace_elapsed_ms=None,
        )

    alloc = allocated_quantity(coord, owner_id=lifecycle.owner_id, token_id=lifecycle.token_id)
    venue = venue_reported_quantity(coord, lifecycle.token_id)
    sellable = sellable_quantity(coord, lifecycle.token_id)

    if alloc != lifecycle_qty:
        tracker.reset()
        return PositionReconciliationResult(
            status=RECON_MISMATCH,
            lifecycle_qty=lifecycle_qty,
            allocated_qty=alloc,
            venue_qty=venue,
            sellable_qty=sellable,
            grace_elapsed_ms=None,
            reason="allocation_lifecycle_mismatch",
            fail_closed=True,
        )

    if lifecycle_qty <= 0:
        tracker.reset()
        return PositionReconciliationResult(
            status=RECON_CONSISTENT,
            lifecycle_qty=lifecycle_qty,
            allocated_qty=alloc,
            venue_qty=venue,
            sellable_qty=sellable,
            grace_elapsed_ms=None,
        )

    if venue >= lifecycle_qty:
        tracker.reset()
        return PositionReconciliationResult(
            status=RECON_CONSISTENT,
            lifecycle_qty=lifecycle_qty,
            allocated_qty=alloc,
            venue_qty=venue,
            sellable_qty=sellable,
            grace_elapsed_ms=None,
        )

    if tracker.first_lag_mono is None:
        tracker.first_lag_mono = now_mono
    grace_ms = (now_mono - tracker.first_lag_mono) * 1000.0
    if grace_ms <= cfg.venue_sync_grace_ms:
        return PositionReconciliationResult(
            status=RECON_VENUE_LAG_EXPECTED,
            lifecycle_qty=lifecycle_qty,
            allocated_qty=alloc,
            venue_qty=venue,
            sellable_qty=sellable,
            grace_elapsed_ms=grace_ms,
            reason="venue_behind_confirmed_fill",
        )

    return PositionReconciliationResult(
        status=RECON_UNRESOLVED,
        lifecycle_qty=lifecycle_qty,
        allocated_qty=alloc,
        venue_qty=venue,
        sellable_qty=sellable,
        grace_elapsed_ms=grace_ms,
        reason="venue_lag_exceeded_grace",
        fail_closed=True,
    )


def safe_exit_quantity(
    lifecycle: ZGapLifecycleState,
    *,
    planned: Decimal,
    recon: PositionReconciliationResult,
) -> Decimal:
    """Never sell more than confirmed owned quantity."""
    qty = min(planned, lifecycle.active_quantity, recon.allocated_qty)
    if recon.sellable_qty > 0:
        qty = min(qty, recon.sellable_qty)
    elif recon.status == RECON_VENUE_LAG_EXPECTED:
        qty = min(qty, lifecycle.active_quantity, recon.allocated_qty)
    return max(Decimal("0"), qty)


def duplicate_fill_idempotent(
    coord: RuntimeCoordinator,
    *,
    owner_id: str,
    token_id: TokenId,
    qty: Decimal,
    before_qty: Decimal,
) -> bool:
    """Return True when allocation already reflects the fill (idempotent replay)."""
    ledger = coord.allocation_ledger
    if ledger is None:
        return False
    after = ledger.get_allocated(owner_id, token_id)
    return after >= before_qty + qty
