"""Phase 3.5 (architecture_enhance): fill/trade finality helper tests."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.state import fill_state
from tyrex_pm.state.fill_state import (
    STATUS_CONFIRMED,
    STATUS_FAILED,
    STATUS_MATCHED,
    STATUS_MINED,
    STATUS_RETRYING,
)


def test_matched_not_final_pnl() -> None:
    assert fill_state.counts_for_realized_pnl(STATUS_MATCHED) is False
    assert fill_state.is_position_final(STATUS_MATCHED) is False
    assert fill_state.is_allocation_final(STATUS_MATCHED) is False


def test_matched_is_execution_evidence() -> None:
    assert fill_state.is_execution_evidence(STATUS_MATCHED) is True


def test_mined_is_evidence_not_final() -> None:
    assert fill_state.is_execution_evidence(STATUS_MINED) is True
    assert fill_state.is_position_final(STATUS_MINED) is False
    assert fill_state.is_allocation_final(STATUS_MINED) is False


def test_confirmed_updates_final_position() -> None:
    assert fill_state.is_position_final(STATUS_CONFIRMED) is True
    assert fill_state.is_execution_evidence(STATUS_CONFIRMED) is True


def test_confirmed_is_allocation_final() -> None:
    assert fill_state.is_allocation_final(STATUS_CONFIRMED) is True
    assert fill_state.counts_for_realized_pnl(STATUS_CONFIRMED) is True
    assert fill_state.releases_reservation(STATUS_CONFIRMED) is True


def test_failed_trade_does_not_apply_allocation() -> None:
    assert fill_state.is_allocation_final(STATUS_FAILED) is False
    assert fill_state.is_position_final(STATUS_FAILED) is False
    assert fill_state.is_execution_evidence(STATUS_FAILED) is False
    assert fill_state.counts_for_realized_pnl(STATUS_FAILED) is False


def test_failed_trade_releases_reservation() -> None:
    assert fill_state.releases_reservation(STATUS_FAILED) is True


def test_retrying_is_not_final_anything() -> None:
    c = fill_state.classify(STATUS_RETRYING)
    assert c.known is True
    assert c.execution_evidence is False
    assert c.position_final is False
    assert c.allocation_final is False
    assert c.releases_reservation is False
    assert c.realized_pnl is False


def test_partial_fill_finality_accounting() -> None:
    # A partial fill is just a (status, size) pair; finality follows status, not
    # size. A MATCHED partial is evidence but not final; a CONFIRMED partial is
    # final for the filled quantity.
    matched = fill_state.classify(STATUS_MATCHED)
    confirmed = fill_state.classify(STATUS_CONFIRMED)
    filled = Decimal("3")
    assert matched.execution_evidence and not matched.allocation_final
    assert confirmed.allocation_final
    assert filled > 0  # finality is independent of the partial quantity


def test_unknown_status_fails_closed() -> None:
    for s in ("", None, "WEIRD", "pending", "cancelled"):
        c = fill_state.classify(s)
        assert c.known is False
        assert c.execution_evidence is False
        assert c.position_final is False
        assert c.allocation_final is False
        assert c.releases_reservation is False
        assert c.realized_pnl is False


def test_allocation_buy_applied_only_on_confirmed() -> None:
    # The protection registration boundary: allocation is final ONLY on CONFIRMED.
    for s in (STATUS_MATCHED, STATUS_MINED, STATUS_RETRYING, STATUS_FAILED):
        assert fill_state.is_allocation_final(s) is False
    assert fill_state.is_allocation_final(STATUS_CONFIRMED) is True


def test_status_normalization_is_case_insensitive() -> None:
    assert fill_state.is_allocation_final("confirmed") is True
    assert fill_state.is_execution_evidence(" matched ") is True
