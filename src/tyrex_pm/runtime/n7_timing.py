"""Frozen N7 Scope A timing ladder numerics (derived from N1/N4/N5 + margin).

Evidence basis (see n1_acceptance_report / n5 shadow config):
- Boundary recv lag observed ~1.6–5.5 s → ack timeout 15 s with margin.
- CL/BN skew p95 ~2.6 s; monitor band 3–5 s → keep ≥5 s slack in flatten window.
- N5 ``flatten_before_event_end_s=20`` → safety buffer 30 s.
- Prep lead recommendation ≥30–60 s → last entry closes 180 s before end
  on a 300 s BTC five-minute window (≈120 s usable entry after open).

These values are production-frozen for N7A/N7B. Changing them requires a new
acceptance + new authorization fingerprint.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from tyrex_pm.runtime.scope_a_ladder import ScopeATimingLadder

# Status string consumed by CLI / reports.
PRODUCTION_TIMING_VALUES_STATUS = "FROZEN_FOR_N7"

# Offsets before authoritative event_end (seconds).
LAST_ALLOWED_ENTRY_BEFORE_END_S = 180.0
DISCRETIONARY_EXIT_CUTOFF_BEFORE_END_S = 120.0
MANDATORY_FLATTEN_START_BEFORE_END_S = 90.0
RESIDUAL_OPERATOR_DEADLINE_BEFORE_END_S = 45.0
EVENT_END_SAFETY_BUFFER_S = 30.0
ACKNOWLEDGMENT_TIMEOUT_S = 15.0
CANCEL_RECON_BUDGET_S = 10.0

# Bounded exit ladder budgets.
EXIT_RETRY_MAX_ATTEMPTS = 3
EXIT_RETRY_TIME_BUDGET_MS = 60_000
ACK_TIMEOUT_MS = 15_000


@dataclass(frozen=True, kw_only=True)
class N7TimingFreeze:
    last_allowed_entry_before_end_s: float = LAST_ALLOWED_ENTRY_BEFORE_END_S
    discretionary_exit_cutoff_before_end_s: float = (
        DISCRETIONARY_EXIT_CUTOFF_BEFORE_END_S
    )
    mandatory_flatten_start_before_end_s: float = MANDATORY_FLATTEN_START_BEFORE_END_S
    residual_operator_deadline_before_end_s: float = (
        RESIDUAL_OPERATOR_DEADLINE_BEFORE_END_S
    )
    event_end_safety_buffer_s: float = EVENT_END_SAFETY_BUFFER_S
    acknowledgment_timeout_s: float = ACKNOWLEDGMENT_TIMEOUT_S
    cancel_recon_budget_s: float = CANCEL_RECON_BUDGET_S
    exit_retry_max_attempts: int = EXIT_RETRY_MAX_ATTEMPTS
    exit_retry_time_budget_ms: int = EXIT_RETRY_TIME_BUDGET_MS
    ack_timeout_ms: int = ACK_TIMEOUT_MS

    def fingerprint_payload(self) -> dict[str, Any]:
        return {
            "last_allowed_entry_before_end_s": self.last_allowed_entry_before_end_s,
            "discretionary_exit_cutoff_before_end_s": (
                self.discretionary_exit_cutoff_before_end_s
            ),
            "mandatory_flatten_start_before_end_s": (
                self.mandatory_flatten_start_before_end_s
            ),
            "residual_operator_deadline_before_end_s": (
                self.residual_operator_deadline_before_end_s
            ),
            "event_end_safety_buffer_s": self.event_end_safety_buffer_s,
            "acknowledgment_timeout_s": self.acknowledgment_timeout_s,
            "cancel_recon_budget_s": self.cancel_recon_budget_s,
            "exit_retry_max_attempts": self.exit_retry_max_attempts,
            "exit_retry_time_budget_ms": self.exit_retry_time_budget_ms,
            "ack_timeout_ms": self.ack_timeout_ms,
            "status": PRODUCTION_TIMING_VALUES_STATUS,
        }

    def build_ladder(self, *, event_end: datetime) -> ScopeATimingLadder:
        return ScopeATimingLadder(
            event_end=event_end,
            last_allowed_entry_before_end=timedelta(
                seconds=self.last_allowed_entry_before_end_s
            ),
            discretionary_exit_cutoff_before_end=timedelta(
                seconds=self.discretionary_exit_cutoff_before_end_s
            ),
            mandatory_flatten_start_before_end=timedelta(
                seconds=self.mandatory_flatten_start_before_end_s
            ),
            residual_operator_deadline_before_end=timedelta(
                seconds=self.residual_operator_deadline_before_end_s
            ),
            event_end_safety_buffer=timedelta(seconds=self.event_end_safety_buffer_s),
            acknowledgment_timeout=timedelta(seconds=self.acknowledgment_timeout_s),
            cancel_recon_budget=timedelta(seconds=self.cancel_recon_budget_s),
        )


N7_TIMING = N7TimingFreeze()
