"""Protection exit sizing (P4 architecture_enhance).

Enforces the global SELL clamp invariant:

    final_size = min(planned_size, owner_allocation, venue_available_to_sell)

The planner-side and risk-side checks remain authoritative; this is the
protection layer's own clamp so it never *asks* to sell more than it owns.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.protection.config import (
    SIZE_MODE_FIXED,
    SIZE_MODE_FULL,
    SIZE_MODE_PERCENT,
    ProtectionPolicy,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.exit_lifecycle import inventory_snapshot


@dataclass(frozen=True)
class ExitSizing:
    planned_before_clamp: Decimal
    owner_allocation: Decimal
    venue_available: Decimal
    final_size: Decimal

    def to_evidence(self) -> dict[str, str]:
        return {
            "planned_before_clamp": str(self.planned_before_clamp),
            "owner_allocation": str(self.owner_allocation),
            "venue_available": str(self.venue_available),
            "final_size": str(self.final_size),
        }


def _planned_from_policy(policy: ProtectionPolicy, owner_allocation: Decimal) -> Decimal:
    if policy.size_mode == SIZE_MODE_FULL:
        return owner_allocation
    if policy.size_mode == SIZE_MODE_PERCENT:
        return owner_allocation * (policy.percent or Decimal("0"))
    if policy.size_mode == SIZE_MODE_FIXED:
        return policy.fixed_size or Decimal("0")
    return Decimal("0")


def compute_exit_sizing(
    coord: RuntimeCoordinator,
    *,
    owner_id: str,
    token_id: TokenId,
    policy: ProtectionPolicy,
) -> ExitSizing:
    ledger = coord.allocation_ledger
    owner_allocation = (
        ledger.get_available_allocated(owner_id, token_id) if ledger is not None else Decimal("0")
    )
    snap = inventory_snapshot(coord, token_id)
    venue_available = Decimal(snap["available_to_sell"])
    planned = _planned_from_policy(policy, owner_allocation)
    final_size = min(planned, owner_allocation, venue_available)
    if final_size < 0:
        final_size = Decimal("0")
    return ExitSizing(
        planned_before_clamp=planned,
        owner_allocation=owner_allocation,
        venue_available=venue_available,
        final_size=final_size,
    )
