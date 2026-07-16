"""Execution outcome taxonomy tests (D3.b)."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.strategies.z_gap.execution_outcomes import (
    OUTCOME_BLOCKED,
    OUTCOME_FULL_FILL,
    OUTCOME_PARTIAL_FILL,
    OUTCOME_REJECTED,
    OUTCOME_UNRESOLVED,
    OUTCOME_ZERO_FILL,
    classify_entry_outcome,
)


def test_blocked_before_submission() -> None:
    o = classify_entry_outcome(blocked=True, blocked_reason="z_gap_entry_blocked_ptb_unusable")
    assert o.category == OUTCOME_BLOCKED
    assert o.reject_stage == "pre_submit"


def test_rejected_after_submission() -> None:
    o = classify_entry_outcome(
        rejected=True,
        submitted=True,
        reject_stage="risk",
        reject_code="RISK_DENIED",
        requested_qty=Decimal("5"),
    )
    assert o.category == OUTCOME_REJECTED


def test_zero_fill() -> None:
    o = classify_entry_outcome(submitted=True, acknowledged=True, requested_qty=Decimal("5"), filled_qty=Decimal("0"))
    assert o.category == OUTCOME_ZERO_FILL


def test_partial_fill() -> None:
    o = classify_entry_outcome(
        submitted=True,
        acknowledged=True,
        requested_qty=Decimal("8"),
        filled_qty=Decimal("3"),
        avg_price=Decimal("0.62"),
    )
    assert o.category == OUTCOME_PARTIAL_FILL


def test_full_fill() -> None:
    o = classify_entry_outcome(
        submitted=True,
        acknowledged=True,
        requested_qty=Decimal("8"),
        filled_qty=Decimal("8"),
    )
    assert o.category == OUTCOME_FULL_FILL


def test_unresolved() -> None:
    o = classify_entry_outcome(unresolved=True, submitted=True, requested_qty=Decimal("5"))
    assert o.category == OUTCOME_UNRESOLVED


def test_fact_payload_fields() -> None:
    o = classify_entry_outcome(
        submitted=True,
        acknowledged=True,
        requested_qty=Decimal("5"),
        filled_qty=Decimal("5"),
        client_order_id="c1",
        venue_order_id="v1",
        order_status="matched",
    )
    p = o.to_fact_payload()
    assert p["client_order_id"] == "c1"
    assert p["venue_order_id"] == "v1"
    assert p["terminal_interpretation"]
