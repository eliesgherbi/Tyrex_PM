"""Shared reason -> retry-class table (replay and live)."""

from __future__ import annotations

import pytest

from tyrex_pm.execution.outcome_reasons import (
    REASON_CLASSES,
    classify_reason,
    classify_reasons,
    is_retryable,
)
from tyrex_pm.strategies.outcomes import RetryClass as C


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("book_stale", C.RETRY_WHEN_CLEARED),
        ("FINAL_GATE_REJECTED:entry_price_moved", C.RETRY_WHEN_CLEARED),
        ("FINAL_GATE_REJECTED:ENTRY_CUTOFF_PASSED", C.END_EPISODE),
        ("PREPARATION:timeout", C.RETRY_WHEN_CLEARED),
        ("NO_FILL_CONFIRMED", C.RETRY_AFTER_ZERO_FILL),
        ("unfilled_cancelled", C.RETRY_AFTER_ZERO_FILL),
        ("DISPATCH_UNKNOWN", C.BLOCK_UNTIL_RECONCILED),
        ("STRATEGY_FEE_CURVE_MISMATCH", C.PAUSE_ENTRIES),
        ("entry_capability_blocked:BOOKS_NOT_READY", C.RETRY_WHEN_CLEARED),
        ("entry_capability_blocked:KILL_SWITCH_ACTIVE,BOOKS_NOT_READY", C.PAUSE_ENTRIES),
        ("FEE_SCHEDULE_READ_FAILED:TimeoutError", C.RETRY_WHEN_CLEARED),
        ("book_desynced", C.RETRY_WHEN_CLEARED),
    ],
)
def test_reason_classes(reason: str, expected: C) -> None:
    assert classify_reason(reason) is expected


def test_unknown_or_missing_reasons_never_fast_retry() -> None:
    assert classify_reason("SOMETHING_NOBODY_REVIEWED") is C.PAUSE_ENTRIES
    assert classify_reason(None) is C.PAUSE_ENTRIES
    assert not is_retryable(classify_reason("SOMETHING_NOBODY_REVIEWED"))


def test_multi_reason_refusal_takes_the_most_restrictive_class() -> None:
    assert classify_reasons(("book_stale", "MAX_UNRESOLVED_COUNT")) is C.PAUSE_ENTRIES
    assert classify_reasons(("SETTLEMENT_EPISODE_NEEDS_RECONCILIATION", "TRIAL_LOSS_BUDGET")) is (
        C.BLOCK_UNTIL_RECONCILED
    )


def test_every_table_entry_has_a_known_class() -> None:
    assert set(REASON_CLASSES.values()) <= set(C)
