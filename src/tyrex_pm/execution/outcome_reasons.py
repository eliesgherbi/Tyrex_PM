"""One reason -> retry-class table for replay and live entry outcomes.

Reasons come from four sources: the host before the lifecycle opens (account state,
cutoff, settlement gate and its governor/constraint sub-reasons), the final dispatch
gate, local order preparation, and the venue (or the replay engine standing in for it).
Unknown reasons are classified conservatively as ``PAUSE_ENTRIES``: a reason nobody has
reviewed must never cause a fast retry loop.
"""

from __future__ import annotations

from tyrex_pm.strategies.outcomes import RetryClass, is_retryable

C = RetryClass

# Exact reason codes.
REASON_CLASSES: dict[str, RetryClass] = {
    # Host, before any execution session exists.
    "ACCOUNT_STATE_NOT_AUTHORITATIVE": C.RETRY_WHEN_CLEARED,
    "ENTRY_CUTOFF_PASSED": C.END_EPISODE,
    "ACTIVE_EXECUTION_BINDING_NOT_PREPARED": C.RETRY_WHEN_CLEARED,
    "PRIOR_ATTEMPT_UNRESOLVED": C.BLOCK_UNTIL_RECONCILED,
    # Settlement constraints (verify_constraints).
    "FEE_SCHEDULE_MISSING": C.PAUSE_ENTRIES,
    "FEE_SCHEDULE_UNPARSED": C.PAUSE_ENTRIES,
    "FEE_SCHEDULE_NOT_TAKER_ONLY": C.PAUSE_ENTRIES,
    "FEE_SCHEDULE_UNSUPPORTED": C.PAUSE_ENTRIES,
    "STRATEGY_FEE_CURVE_MISMATCH": C.PAUSE_ENTRIES,
    "STRATEGY_FEE_CURVE_UNKNOWN": C.PAUSE_ENTRIES,
    "TICK_SIZE_UNKNOWN": C.PAUSE_ENTRIES,
    "MIN_ORDER_SIZE_UNKNOWN": C.PAUSE_ENTRIES,
    "OUTCOME_INDEX_UNKNOWN": C.PAUSE_ENTRIES,
    # Governor (durable ledger budgets).
    "SETTLEMENT_EPISODE_NEEDS_RECONCILIATION": C.BLOCK_UNTIL_RECONCILED,
    "SETTLEMENT_MANUAL_INTERVENTION_OPEN": C.PAUSE_ENTRIES,
    "EPISODE_ALREADY_EXISTS_FOR_WINDOW": C.END_EPISODE,
    "OPEN_EPISODE_EXISTS_FOR_WINDOW": C.BLOCK_UNTIL_RECONCILED,
    "WINDOW_EPISODE_CAP": C.END_EPISODE,
    "WINDOW_ATTEMPT_CAP": C.END_EPISODE,
    "WINDOW_POST_CAP": C.END_EPISODE,
    "EPISODE_ATTEMPT_CAP": C.END_EPISODE,
    "EPISODE_POST_CAP": C.END_EPISODE,
    "EPISODE_PREPARE_FAILURE_CAP": C.END_EPISODE,
    "PROPOSED_DEBIT_NOT_POSITIVE": C.END_EPISODE,
    "MAX_DEBIT_PER_EPISODE": C.END_EPISODE,
    "MAX_EPISODES": C.PAUSE_ENTRIES,
    "MAX_UNRESOLVED_COUNT": C.PAUSE_ENTRIES,
    "MAX_UNRESOLVED_COST": C.PAUSE_ENTRIES,
    "TRIAL_LOSS_BUDGET": C.PAUSE_ENTRIES,
    "DAILY_LOSS_LIMIT": C.PAUSE_ENTRIES,
    "MAX_OPEN_WORST_CASE_LOSS": C.PAUSE_ENTRIES,
    "MAX_RESERVED_DEBIT": C.PAUSE_ENTRIES,
    "MAX_UNREDEEMED_COUNT": C.PAUSE_ENTRIES,
    "MAX_UNREDEEMED_COST": C.PAUSE_ENTRIES,
    "AVAILABLE_COLLATERAL_INSUFFICIENT": C.PAUSE_ENTRIES,
    "CONSECUTIVE_LOSS_PAUSE": C.PAUSE_ENTRIES,
    "KILL_SWITCH_ACTIVE": C.PAUSE_ENTRIES,
    "RUN_STOP_REQUESTED": C.END_EPISODE,
    # Final dispatch gate (dispatch_check).
    "FEATURE_NOT_READY": C.RETRY_WHEN_CLEARED,
    "EWMA_NOT_READY": C.RETRY_WHEN_CLEARED,
    "STARTUP_ASSESSMENT_EXPIRED": C.RETRY_WHEN_CLEARED,
    "STARTUP_STREAM_GAP": C.RETRY_WHEN_CLEARED,
    "STARTUP_RECHECK_FAILED": C.RETRY_WHEN_CLEARED,
    "book_view_unavailable": C.RETRY_WHEN_CLEARED,
    "active_binding_changed": C.END_EPISODE,
    "token_not_in_active_market": C.END_EPISODE,
    "book_stale": C.RETRY_WHEN_CLEARED,
    "book_tick_size_changed_after_signing": C.RETRY_WHEN_CLEARED,
    "no_entry_ask": C.RETRY_WHEN_CLEARED,
    "entry_price_moved": C.RETRY_WHEN_CLEARED,
    "entry_depth_below_minimum": C.RETRY_WHEN_CLEARED,
    "candidate_stale_after_signing": C.RETRY_WHEN_CLEARED,
    "candidate_monotonic_time_invalid": C.PAUSE_ENTRIES,
    # Venue / replay engine terminal outcomes.
    "NO_FILL_CONFIRMED": C.RETRY_AFTER_ZERO_FILL,
    "ORDER_REJECTED": C.RETRY_AFTER_ZERO_FILL,
    "unfilled_cancelled": C.RETRY_AFTER_ZERO_FILL,
    "invalid_book": C.RETRY_WHEN_CLEARED,
    "below_minimum": C.RETRY_AFTER_ZERO_FILL,
    "fok_insufficient_depth": C.RETRY_AFTER_ZERO_FILL,
    "off_tick": C.END_EPISODE,
    "insufficient_available_cash": C.PAUSE_ENTRIES,
    "insufficient_unreserved_inventory": C.PAUSE_ENTRIES,
    "adverse_ticks_without_tick": C.PAUSE_ENTRIES,
    "recorded_fee_curve_unavailable": C.PAUSE_ENTRIES,
    "duplicate_order_id": C.PAUSE_ENTRIES,
    "arrival_outside_recording": C.END_EPISODE,
    "post_only_would_cross": C.RETRY_WHEN_CLEARED,
    # Unknown dispatch outcome.
    "DISPATCH_UNKNOWN": C.BLOCK_UNTIL_RECONCILED,
    "SUBMISSION_AMBIGUOUS": C.BLOCK_UNTIL_RECONCILED,
}

# Prefix rules, checked in order, for reasons that carry details after a separator.
PREFIX_CLASSES: tuple[tuple[str, RetryClass], ...] = (
    ("entry_capability_blocked:", C.RETRY_WHEN_CLEARED),
    ("book_", C.RETRY_WHEN_CLEARED),  # book_<sync health>, e.g. book_desynced
    ("FEE_SCHEDULE_READ_FAILED:", C.RETRY_WHEN_CLEARED),
    ("PREPARATION:", C.RETRY_WHEN_CLEARED),
    ("FINAL_GATE_REJECTED:", C.RETRY_WHEN_CLEARED),  # refined by its inner reason
)

# Capability blockers that make an entry refusal structural instead of transient.
STRUCTURAL_CAPABILITY_BLOCKERS = frozenset(
    {
        "KILL_SWITCH_ACTIVE",
        "LIVE_NOT_REQUESTED",
        "MANUAL_INTERVENTION",
        "AMBIGUOUS_PRIOR_SUBMISSION",
        "UNRESOLVED_PRIOR_MARKET_SESSION",
        "MULTIPLE_RESTORED_SESSIONS",
        "STARTUP_ACTION_REQUIRED",
        "STOP_REQUESTED",
    }
)

# Classes ranked from most to least restrictive; a multi-reason refusal takes the worst.
_SEVERITY = (
    C.BLOCK_UNTIL_RECONCILED,
    C.PAUSE_ENTRIES,
    C.END_EPISODE,
    C.RETRY_AFTER_ZERO_FILL,
    C.RETRY_WHEN_CLEARED,
    C.NONE,
)


def classify_reason(reason: str | None) -> RetryClass:
    """Retry class of one reason code (conservative for unknown codes)."""
    if not reason:
        return C.PAUSE_ENTRIES
    if reason in REASON_CLASSES:
        return REASON_CLASSES[reason]
    if reason.startswith("FINAL_GATE_REJECTED:"):
        return classify_reason(reason.split(":", 1)[1])
    if reason.startswith("entry_capability_blocked:"):
        blockers = set(filter(None, reason.split(":", 1)[1].split(",")))
        if blockers & STRUCTURAL_CAPABILITY_BLOCKERS:
            return C.PAUSE_ENTRIES
        return C.RETRY_WHEN_CLEARED
    for prefix, retry_class in PREFIX_CLASSES:
        if reason.startswith(prefix):
            return retry_class
    return C.PAUSE_ENTRIES


def classify_reasons(reasons: tuple[str, ...] | list[str]) -> RetryClass:
    """Most restrictive class across several reasons (e.g. governor sub-reasons)."""
    if not reasons:
        return C.PAUSE_ENTRIES
    classes = {classify_reason(r) for r in reasons}
    return next(c for c in _SEVERITY if c in classes)


__all__ = ["REASON_CLASSES", "classify_reason", "classify_reasons", "is_retryable"]
