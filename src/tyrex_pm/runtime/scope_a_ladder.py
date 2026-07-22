"""Scope A timing ladder — structure validated; production numerics frozen in n7_timing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True, kw_only=True)
class ScopeATimingLadder:
    """Deadlines relative to authoritative ``event_end``.

    Production values are frozen in ``n7_timing.N7_TIMING`` for N7.
    Tests may still construct explicit timedeltas.
    Late entry is skipped. Scope A never silently becomes hold-to-resolution.
    """

    event_end: datetime
    last_allowed_entry_before_end: timedelta
    discretionary_exit_cutoff_before_end: timedelta
    mandatory_flatten_start_before_end: timedelta
    acknowledgment_timeout: timedelta
    cancel_recon_budget: timedelta
    residual_operator_deadline_before_end: timedelta
    event_end_safety_buffer: timedelta

    def __post_init__(self) -> None:
        # Ordering: entry window closes before discretionary exit, which closes
        # before mandatory flatten, which is before residual deadline, before end.
        if self.last_allowed_entry_before_end < self.discretionary_exit_cutoff_before_end:
            raise ValueError(
                "last_allowed_entry_before_end must be >= discretionary_exit_cutoff_before_end"
            )
        if (
            self.discretionary_exit_cutoff_before_end
            < self.mandatory_flatten_start_before_end
        ):
            raise ValueError(
                "discretionary_exit_cutoff_before_end must be >= mandatory_flatten_start_before_end"
            )
        if (
            self.mandatory_flatten_start_before_end
            < self.residual_operator_deadline_before_end
        ):
            raise ValueError(
                "mandatory_flatten_start_before_end must be >= residual_operator_deadline_before_end"
            )
        if self.residual_operator_deadline_before_end < self.event_end_safety_buffer:
            raise ValueError(
                "residual_operator_deadline_before_end must be >= event_end_safety_buffer"
            )
        if self.acknowledgment_timeout <= timedelta(0):
            raise ValueError("acknowledgment_timeout must be > 0")
        if self.cancel_recon_budget <= timedelta(0):
            raise ValueError("cancel_recon_budget must be > 0")

    @property
    def last_allowed_entry_at(self) -> datetime:
        return self.event_end - self.last_allowed_entry_before_end

    @property
    def discretionary_exit_cutoff_at(self) -> datetime:
        return self.event_end - self.discretionary_exit_cutoff_before_end

    @property
    def mandatory_flatten_start_at(self) -> datetime:
        return self.event_end - self.mandatory_flatten_start_before_end

    @property
    def residual_operator_deadline_at(self) -> datetime:
        return self.event_end - self.residual_operator_deadline_before_end

    @property
    def hard_stop_at(self) -> datetime:
        return self.event_end - self.event_end_safety_buffer

    def entry_allowed(self, now: datetime) -> bool:
        return now < self.last_allowed_entry_at

    def requires_mandatory_flatten(self, now: datetime) -> bool:
        return now >= self.mandatory_flatten_start_at

    def past_hard_stop(self, now: datetime) -> bool:
        return now >= self.hard_stop_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "live_scope": "A",
            "event_end": self.event_end.isoformat(),
            "last_allowed_entry_at": self.last_allowed_entry_at.isoformat(),
            "discretionary_exit_cutoff_at": self.discretionary_exit_cutoff_at.isoformat(),
            "mandatory_flatten_start_at": self.mandatory_flatten_start_at.isoformat(),
            "residual_operator_deadline_at": self.residual_operator_deadline_at.isoformat(),
            "hard_stop_at": self.hard_stop_at.isoformat(),
            "acknowledgment_timeout_s": self.acknowledgment_timeout.total_seconds(),
            "cancel_recon_budget_s": self.cancel_recon_budget.total_seconds(),
            "production_values": "FROZEN_FOR_N7",
            "hold_to_resolution": False,
        }


# Production numerics live in n7_timing; this alias keeps N6 CLI compatible.
PRODUCTION_TIMING_VALUES_STATUS = "FROZEN_FOR_N7"
