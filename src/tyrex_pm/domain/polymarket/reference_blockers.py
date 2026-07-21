"""N3 readiness / blocker reason codes (precise — never collapse to not_ready)."""

from __future__ import annotations

from enum import Enum


class ReferenceBlockerReason(str, Enum):
    NO_BOUNDARY_CANDIDATE = "no_boundary_candidate"
    EXACT_CANDIDATE_ABSENT = "exact_candidate_absent"
    AMBIGUOUS_FALLBACK_CANDIDATES = "ambiguous_fallback_candidates"
    INCOMPLETE_FALLBACK_CANDIDATES = "incomplete_fallback_candidates"
    NO_CAUSAL_BINANCE_PAIR = "no_causal_binance_pair"
    STALE_REFERENCE = "stale_reference"
    CLOCK_UNSYNCHRONIZED = "unsynchronized_clock"
    CLOCK_DEGRADED = "degraded_clock"
    ATTESTATION_UNAVAILABLE = "attestation_unavailable"
    ATTESTATION_MISMATCH = "attestation_mismatch"
    LATE_EVENT_AFTER_SEAL = "late_event_after_seal"
    SOURCE_RECONNECT_GAP = "source_reconnect_gap"
    THRESHOLD_NOT_CONFIGURED = "threshold_not_configured"
    CONFLICTING_DUPLICATE = "conflicting_duplicate"
    SEALED_IMMUTABLE = "sealed_immutable"
