"""R5.1 entry/exit retry controller unit tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tyrex_pm.runtime.retry_controller import (
    EntryRetryPhase,
    ExitRetryPhase,
    RetryConfig,
    RetryController,
)

T0 = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def test_entry_plan_failure_enters_retry_wait() -> None:
    ctl = RetryController(config=RetryConfig(entry_cooldown=timedelta(seconds=5), entry_max_attempts=3))
    ctl.note_entry_attempt(now=T0, direction="UP", book_fingerprint="a", reason="MOMENTUM")
    ctl.note_entry_plan_failed(reason="INSUFFICIENT_DEPTH", now=T0)
    assert ctl.entry.phase is EntryRetryPhase.RETRY_WAIT
    allowed, reason = ctl.entry_allowed(now=T0 + timedelta(seconds=1), book_fingerprint="a")
    assert not allowed
    assert reason == "ENTRY_COOLDOWN"


def test_binance_tick_during_cooldown_no_retry() -> None:
    ctl = RetryController(config=RetryConfig(entry_cooldown=timedelta(seconds=10)))
    ctl.note_entry_attempt(now=T0, direction="UP", book_fingerprint="a", reason="x")
    ctl.note_entry_plan_failed(reason="x", now=T0)
    # Same book fingerprint after 1s — still blocked
    ok, why = ctl.entry_allowed(now=T0 + timedelta(seconds=1), book_fingerprint="a")
    assert not ok
    assert why in {"ENTRY_COOLDOWN", "ENTRY_WAIT_TRIGGER"}


def test_cooldown_and_book_change_permits_one_retry() -> None:
    ctl = RetryController(config=RetryConfig(entry_cooldown=timedelta(seconds=2)))
    ctl.note_entry_attempt(now=T0, direction="UP", book_fingerprint="a", reason="x")
    ctl.note_entry_plan_failed(reason="x", now=T0)
    ok, why = ctl.entry_allowed(
        now=T0 + timedelta(seconds=3), book_fingerprint="b"
    )
    assert ok
    assert why == "RETRY_TRIGGER"


def test_entry_attempt_cap() -> None:
    ctl = RetryController(config=RetryConfig(entry_max_attempts=2, entry_cooldown=timedelta(seconds=1)))
    for i in range(2):
        ctl.note_entry_attempt(
            now=T0 + timedelta(seconds=i * 2),
            direction="UP",
            book_fingerprint=str(i),
            reason="x",
        )
        ctl.note_entry_plan_failed(reason="x", now=T0 + timedelta(seconds=i * 2))
    assert ctl.entry.phase is EntryRetryPhase.CAP_REACHED
    ok, why = ctl.entry_allowed(now=T0 + timedelta(seconds=60), book_fingerprint="z")
    assert not ok
    assert why == "ENTRY_ATTEMPT_CAP"


def test_market_reset_clears_retry() -> None:
    ctl = RetryController()
    ctl.note_entry_attempt(now=T0, direction="UP", book_fingerprint="a", reason="x")
    ctl.note_entry_plan_failed(reason="x", now=T0)
    ctl.reset_market()
    assert ctl.entry.phase is EntryRetryPhase.ELIGIBLE
    assert ctl.entry.attempts == 0


def test_exit_outstanding_blocks_until_cooldown() -> None:
    ctl = RetryController(config=RetryConfig(exit_cooldown=timedelta(seconds=5)))
    ctl.note_exit_request(now=T0, reason="SIGNAL_REVERSAL")
    assert ctl.exit_outstanding()
    ok, why = ctl.exit_allowed(now=T0 + timedelta(seconds=1))
    assert not ok
    assert why == "EXIT_REQUESTED"
    ctl.note_exit_plan_failed(reason="INSUFFICIENT_DEPTH", now=T0)
    assert ctl.exit.phase is ExitRetryPhase.EXIT_RETRY_WAIT
    ok2, _ = ctl.exit_allowed(now=T0 + timedelta(seconds=1))
    assert not ok2
    ok3, why3 = ctl.exit_allowed(now=T0 + timedelta(seconds=6))
    assert ok3
    assert why3 == "EXIT_RETRY_TRIGGER"


def test_kill_switch_escalates_exit() -> None:
    ctl = RetryController()
    ctl.note_exit_request(now=T0, reason="SIGNAL_FLAT")
    ok, why = ctl.exit_allowed(now=T0, escalate=True)
    assert ok
    assert why == "ESCALATE_EXIT"
    ctl.note_exit_request(now=T0, reason="KILL_SWITCH", urgency="URGENT", escalate=True)
    assert ctl.exit.urgency == "URGENT"
    assert ctl.exit.phase is ExitRetryPhase.ESCALATED


def test_manual_intervention_after_exhausted_urgent_retries() -> None:
    ctl = RetryController(
        config=RetryConfig(exit_max_normal_retries=2, exit_escalate_after=1, exit_cooldown=timedelta(seconds=1))
    )
    ctl.note_exit_request(now=T0, reason="SIGNAL_REVERSAL", urgency="URGENT", escalate=True)
    ctl.note_exit_plan_failed(reason="ONE_SIDED", now=T0)
    ctl.note_exit_request(
        now=T0 + timedelta(seconds=2), reason="KILL_SWITCH", urgency="URGENT", escalate=True
    )
    ctl.note_exit_plan_failed(reason="ONE_SIDED", now=T0 + timedelta(seconds=2))
    assert ctl.exit.phase is ExitRetryPhase.MANUAL_INTERVENTION
    ok, why = ctl.exit_allowed(now=T0 + timedelta(seconds=100))
    assert not ok
    assert why == "MANUAL_INTERVENTION"
