"""Summarizer delta labels (approved + post-risk execution)."""

from __future__ import annotations

from tyrex_pm.reporting.taxonomy import (
    DELTA_REASON_RISK_PASSED,
    guru_row_delta_reason,
    to_delta_reason,
)


def test_approved_maps_risk_passed() -> None:
    assert to_delta_reason("approved", None) == DELTA_REASON_RISK_PASSED


def test_guru_row_allowed_submit_keeps_risk_passed() -> None:
    d = guru_row_delta_reason(
        risk_allowed=True,
        risk_reason_code="approved",
        strategy_reason_code="guru_entry_candidate",
        gate=None,
        last_execution_outcome="submit",
        last_execution_reason_code="live_order_submit",
    )
    assert d == DELTA_REASON_RISK_PASSED


def test_guru_row_allowed_execution_error_maps_activation_cap() -> None:
    d = guru_row_delta_reason(
        risk_allowed=True,
        risk_reason_code="approved",
        strategy_reason_code="guru_entry_candidate",
        gate=None,
        last_execution_outcome="error",
        last_execution_reason_code="guru_dynamic_activation_cap",
    )
    assert d == "activation_cap"


def test_guru_row_denied_uses_risk_reason() -> None:
    d = guru_row_delta_reason(
        risk_allowed=False,
        risk_reason_code="risk_notional_per_order",
        strategy_reason_code="guru_entry_candidate",
        gate="",
        last_execution_outcome=None,
        last_execution_reason_code=None,
    )
    assert d == "order_deployment_cap"
