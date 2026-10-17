"""When a settlement-reference tick may be used, in live and in replay (one policy).

Two delivery kinds exist for the Chainlink 60 s TWAP series:

* ``live``: pushed as it is published. It drives the opening price (exact boundary tick)
  and the model's latest reported TWAP.
* ``subscription_history``: the recent ticks RTDS sends after a (re)subscription.
  Their source timestamps are in the past; their *availability* is the time the history
  arrived. They may only recover the exact boundary tick of a window whose opening
  price is not sealed yet. They never become the latest price, never feed freshness,
  and never stand in for a nearby timestamp (no interpolation, no substitution).

Arrival bounds at sealing:

* a live boundary tick must arrive within ``max_live_arrival_lag_ms`` of the boundary;
* a recovered boundary tick must arrive within ``history_recovery_deadline_ms``. The
  strategy cannot enter before its entry band (150 s before the end for the baseline),
  so a recovery inside the deadline completes long before it could trade; replay must
  apply the actual arrival time, never the source timestamp.

``reference_availability_v1`` is versioned: replays record the policy id, and
``legacy`` reproduces earlier research (every row applied as live, no arrival bound).
The observed history depth (~58 s) is behaviour, not a retention contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

DELIVERY_LIVE = "live"
DELIVERY_HISTORY = "subscription_history"
POLICY_V1 = "reference_availability_v1"
POLICY_LEGACY = "legacy"


@dataclass(frozen=True)
class ReferenceAvailabilityPolicy:
    policy_id: str = POLICY_V1
    max_live_arrival_lag_ms: int = 5_000
    history_recovery_enabled: bool = True
    # Provisional starting value (operator decision 2026-10-17), not a tuned value.
    history_recovery_deadline_ms: int = 30_000

    def __post_init__(self) -> None:
        if self.policy_id not in (POLICY_V1, POLICY_LEGACY):
            raise ValueError(f"unknown reference availability policy {self.policy_id!r}")
        if self.max_live_arrival_lag_ms <= 0 or self.history_recovery_deadline_ms <= 0:
            raise ValueError("arrival bounds must be positive")
        if self.history_recovery_deadline_ms < self.max_live_arrival_lag_ms:
            raise ValueError("the recovery deadline cannot be tighter than the live bound")

    @classmethod
    def legacy(cls) -> "ReferenceAvailabilityPolicy":
        return cls(policy_id=POLICY_LEGACY, history_recovery_enabled=False)

    @property
    def is_legacy(self) -> bool:
        return self.policy_id == POLICY_LEGACY

    def history_usable_for_boundary(
        self,
        *,
        source_ts: datetime,
        received_at: datetime,
        window_start: datetime,
        already_sealed: bool,
    ) -> bool:
        """May this history tick recover ``window_start``'s opening price?"""
        if self.is_legacy or not self.history_recovery_enabled or already_sealed:
            return False
        if source_ts != window_start:  # exact timestamp only
            return False
        lag_ms = (received_at - window_start).total_seconds() * 1000.0
        return 0 <= lag_ms <= self.history_recovery_deadline_ms

    def arrival_ok(self, *, lag_ms: float | None, recovered_from_history: bool) -> bool:
        """Arrival bound for the tick an opening price is sealed from."""
        if self.is_legacy:
            return True
        if lag_ms is None or lag_ms < 0:
            return False
        bound = (
            self.history_recovery_deadline_ms
            if recovered_from_history
            else self.max_live_arrival_lag_ms
        )
        return lag_ms <= bound

    def bound_ms(self, *, recovered_from_history: bool) -> int:
        return (
            self.history_recovery_deadline_ms
            if recovered_from_history
            else self.max_live_arrival_lag_ms
        )

    def identity(self) -> dict:
        return {
            "policy_id": self.policy_id,
            "max_live_arrival_lag_ms": self.max_live_arrival_lag_ms,
            "history_recovery_enabled": self.history_recovery_enabled,
            "history_recovery_deadline_ms": self.history_recovery_deadline_ms,
        }


def is_history(ingress: object | None) -> bool:
    return getattr(ingress, "delivery_kind", DELIVERY_LIVE) == DELIVERY_HISTORY


def selected_boundary_tick(state: object) -> object | None:
    """The evidence tick a window's selected EXACT candidate came from (provenance)."""
    cand = getattr(state, "selected_candidate", None)
    if cand is None:
        return None
    for row in getattr(state, "evidence", ()):
        tick = row.tick
        if tick.source_ts == cand.chainlink_source_ts and tick.value == cand.value:
            return tick
    return None
