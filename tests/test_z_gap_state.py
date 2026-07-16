"""State machine tests for Z-Gap lifecycle (A0.7)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tyrex_pm.strategies.z_gap.state import (
    InvalidZGapTransition,
    ZGapLifecycleState,
    ZGapPhase,
    assert_startup_state_terminal_or_absent,
)


def _lc() -> ZGapLifecycleState:
    return ZGapLifecycleState(market_id="m1", condition_id="0xabc", owner_id="z_gap")


def test_idle_to_entry_pending() -> None:
    lc = _lc()
    lc.entry_attempted = True
    lc.transition(ZGapPhase.ENTRY_PENDING)
    assert lc.phase == ZGapPhase.ENTRY_PENDING


def test_zero_fill_to_done() -> None:
    lc = _lc()
    lc.transition(ZGapPhase.ENTRY_PENDING)
    lc.transition(ZGapPhase.DONE)
    assert lc.phase == ZGapPhase.DONE


def test_full_fill_to_active() -> None:
    lc = _lc()
    lc.transition(ZGapPhase.ENTRY_PENDING)
    lc.active_quantity = Decimal("8")
    lc.transition(ZGapPhase.ACTIVE)
    assert lc.phase == ZGapPhase.ACTIVE


def test_partial_fill_active_quantity() -> None:
    lc = _lc()
    lc.transition(ZGapPhase.ENTRY_PENDING)
    lc.entry_filled_shares = Decimal("3")
    lc.active_quantity = Decimal("3")
    lc.transition(ZGapPhase.ACTIVE)
    assert lc.active_quantity == Decimal("3")


def test_invalid_transition_rejected() -> None:
    lc = _lc()
    with pytest.raises(InvalidZGapTransition):
        lc.transition(ZGapPhase.ACTIVE)


def test_no_reentry_after_exit() -> None:
    lc = _lc()
    lc.exited_this_window = True
    assert not lc.can_submit_entry()


def test_one_entry_attempt_per_window() -> None:
    lc = _lc()
    lc.entry_attempted = True
    assert not lc.can_submit_entry()


def test_startup_non_terminal_fails_closed() -> None:
    with pytest.raises(RuntimeError):
        assert_startup_state_terminal_or_absent({"phase": "ACTIVE"})
