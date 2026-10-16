"""settlement_value episode state machine: retry, exits and re-entry (driver level).

Uses the synthetic market/snapshot helpers of ``test_settlement_value``. Outcomes are
delivered exactly as a host would deliver them (``on_entry_outcome``/``on_exit_outcome``)
and the position comes only from the context, as in replay and live.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from test_settlement_value import START, run
from test_settlement_value import driver as base_driver

from tyrex_pm.core.intents import EnterIntent, ExitIntent, HoldToResolutionIntent
from tyrex_pm.runtime.config_error import ConfigError
from tyrex_pm.strategies.decisions import StrategyAction
from tyrex_pm.strategies.outcomes import (
    EntryOutcome,
    EntryOutcomeKind,
    ExitOutcome,
    ExitOutcomeKind,
    RetryClass,
)
from tyrex_pm.strategies.settlement_value import reasons as R
from tyrex_pm.strategies.settlement_value.schema import settlement_value_config_from_parameters
from tyrex_pm.strategies.settlement_value.state import EpisodePhase, EpisodeState

AT = START + timedelta(seconds=120)  # tau = 180 s


def driver(**params):
    return base_driver(**params)


def zero_fill(intent, at, reason="NO_FILL_CONFIRMED"):
    return EntryOutcome(
        EntryOutcomeKind.ZERO_FILL, intent.intent_id.value, at, reason=reason,
        retry_class=RetryClass.RETRY_AFTER_ZERO_FILL, posted=True,
    )


def not_sent(intent, at, reason="FINAL_GATE_REJECTED:book_stale", **kw):
    return EntryOutcome(
        EntryOutcomeKind.NOT_SENT, intent.intent_id.value, at, reason=reason,
        retry_class=kw.pop("retry_class", RetryClass.RETRY_WHEN_CLEARED), **kw,
    )


RETRY = {"retry": {"enabled": True, "min_retry_interval_ms": 500}}


# -- feature-off equivalence ---------------------------------------------------------


def test_retry_disabled_zero_fill_closes_the_window_with_legacy_reasons() -> None:
    d = driver()
    (intent,) = run(d, AT).intents
    d.on_entry_outcome(zero_fill(intent, AT))
    later = run(d, AT + timedelta(seconds=5))
    assert later.decision.reason_code == R.EPISODE_CONSUMED and not later.intents
    blocked = run(d, AT + timedelta(seconds=6), ctx_kwargs={"allowed": False})
    assert blocked.decision.reason_code == R.PENDING_ORDER
    assert d.state.phase is EpisodePhase.CLOSED and d.state.window_attempts == 1


def test_first_attempt_intent_keeps_its_original_semantic_key() -> None:
    (intent,) = run(driver(), AT).intents
    assert "attempt_id" not in intent.evidence
    assert intent.evidence["episode_seq"] == 1 and intent.evidence["attempt_seq"] == 1


def test_old_slices_restore_as_a_spent_single_attempt() -> None:
    state = EpisodeState(window_id="w")
    state.restore({"window_id": "w", "entry_emitted": True, "hold_requested": False})
    assert state.phase is EpisodePhase.CLOSED and state.entry_emitted


# -- retry ---------------------------------------------------------------------------


def test_zero_fill_rearms_after_backoff_and_recomputes_the_order() -> None:
    d = driver(**RETRY)
    (first,) = run(d, AT).intents
    d.on_entry_outcome(zero_fill(first, AT))
    assert d.state.phase is EpisodePhase.REARMABLE
    waiting = run(d, AT + timedelta(milliseconds=200))
    assert waiting.decision.reason_code == R.RETRY_BACKOFF and not waiting.intents
    retry = run(d, AT + timedelta(seconds=1), up_ask="0.31")
    (second,) = retry.intents
    assert isinstance(second, EnterIntent)
    assert second.max_price == Decimal("0.31")  # recomputed from the current book
    assert second.evidence["attempt_id"] == "e1a2"
    assert second.intent_id != first.intent_id


def test_uncertain_outcome_blocks_any_further_buy() -> None:
    d = driver(**RETRY)
    (first,) = run(d, AT).intents
    d.on_entry_outcome(
        EntryOutcome(EntryOutcomeKind.UNCERTAIN, first.intent_id.value, AT,
                     reason="DISPATCH_UNKNOWN", retry_class=RetryClass.BLOCK_UNTIL_RECONCILED)
    )
    for step in range(1, 5):
        result = run(d, AT + timedelta(seconds=step))
        assert not result.intents and result.decision.reason_code == R.OUTCOME_UNCERTAIN


def test_pending_attempt_blocks_a_second_buy_before_any_outcome() -> None:
    d = driver(**RETRY)
    run(d, AT)
    again = run(d, AT + timedelta(seconds=1))
    assert not again.intents


@pytest.mark.parametrize(
    ("limit", "reason"),
    [
        ({"max_attempts_per_episode": 2}, R.RETRY_ATTEMPTS_EXHAUSTED),
        ({"max_posts_per_episode": 1}, R.RETRY_POSTS_EXHAUSTED),
        ({"max_retry_duration_s": 0.5}, R.RETRY_DURATION_EXHAUSTED),
    ],
)
def test_retry_ceilings_close_the_episode(limit, reason) -> None:
    d = driver(retry={"enabled": True, "min_retry_interval_ms": 0, **limit})
    at = AT
    for _ in range(4):
        result = run(d, at)
        if not result.intents:
            break
        d.on_entry_outcome(zero_fill(result.intents[0], at))
        at += timedelta(seconds=1)
    assert result.decision.reason_code == reason
    assert d.state.phase is EpisodePhase.CLOSED


def test_repeated_preparation_failures_without_any_post_are_bounded() -> None:
    d = driver(retry={"enabled": True, "min_retry_interval_ms": 0, "service_backoff_ms": 0,
                      "max_prepare_failures_per_episode": 2, "max_attempts_per_episode": 9,
                      "max_attempts_per_window": 9})
    at = AT
    attempts = 0
    for _ in range(9):
        result = run(d, at)
        if not result.intents:
            break
        attempts += 1
        d.on_entry_outcome(not_sent(result.intents[0], at, reason="PREPARATION:x",
                                    prepare_failed=True))
        at += timedelta(seconds=1)
    assert attempts == 2 and result.decision.reason_code == R.RETRY_PREPARE_FAILURES_EXHAUSTED
    assert d.state.episode_posts == 0


def test_service_failures_back_off_exponentially() -> None:
    d = driver(retry={"enabled": True, "min_retry_interval_ms": 0, "service_backoff_ms": 1000,
                      "service_backoff_max_ms": 4000, "max_attempts_per_episode": 9,
                      "max_attempts_per_window": 9, "max_prepare_failures_per_episode": 9,
                      "max_retry_duration_s": 60})
    (first,) = run(d, AT).intents
    d.on_entry_outcome(not_sent(first, AT))
    assert run(d, AT + timedelta(milliseconds=900)).decision.reason_code == R.RETRY_BACKOFF
    (second,) = run(d, AT + timedelta(milliseconds=1000)).intents
    d.on_entry_outcome(not_sent(second, AT + timedelta(seconds=1)))
    held_back = run(d, AT + timedelta(milliseconds=2900))
    assert held_back.decision.reason_code == R.RETRY_BACKOFF  # 2 s after the second


def test_structural_refusal_is_not_retried() -> None:
    d = driver(**RETRY)
    (first,) = run(d, AT).intents
    d.on_entry_outcome(not_sent(first, AT, reason="STRATEGY_FEE_CURVE_MISMATCH",
                                retry_class=RetryClass.PAUSE_ENTRIES))
    later = run(d, AT + timedelta(seconds=5))
    assert not later.intents and later.decision.reason_code.startswith(R.ENTRY_NOT_RETRYABLE)


def test_retry_keeps_the_side_and_stops_when_it_loses_eligibility() -> None:
    d = driver(**RETRY)
    (first,) = run(d, AT).intents
    d.on_entry_outcome(zero_fill(first, AT))
    # Up is now priced out while Down became attractive: the episode stops pursuit.
    result = run(d, AT + timedelta(seconds=1), up_ask="0.80", down_ask="0.30")
    assert not result.intents and result.decision.reason_code == R.RETRY_SIDE_NOT_ELIGIBLE


def test_partial_fill_is_a_position_and_never_a_top_up() -> None:
    d = driver(**RETRY)
    (first,) = run(d, AT).intents
    d.on_entry_outcome(
        EntryOutcome(EntryOutcomeKind.PARTIAL, first.intent_id.value, AT, posted=True,
                     confirmed_shares=Decimal("3"), cost=Decimal("0.9"))
    )
    held = run(d, AT + timedelta(seconds=1), ctx_kwargs={"qty": "3"})
    assert [type(i) for i in held.intents] == [HoldToResolutionIntent]
    assert not any(isinstance(i, EnterIntent) for i in held.intents)


def test_late_outcome_for_an_old_attempt_is_ignored() -> None:
    d = driver(**RETRY)
    (first,) = run(d, AT).intents
    d.on_entry_outcome(zero_fill(first, AT))
    (second,) = run(d, AT + timedelta(seconds=1)).intents
    d.on_entry_outcome(zero_fill(first, AT + timedelta(seconds=1)))  # duplicate / stale
    assert d.state.phase is EpisodePhase.PENDING
    assert d.state.entry_intent_id == second.intent_id.value


def test_restart_restores_window_counters_from_the_ledger() -> None:
    d = driver(retry={"enabled": True, "max_attempts_per_episode": 3,
                      "min_retry_interval_ms": 0})
    d.restore_window_usage(episodes=1, attempts=2, posts=2, filled_episodes=0,
                           latest_attempts=2, latest_state="NO_FILL")
    (third,) = run(d, AT).intents
    assert third.evidence["attempt_id"] == "e1a3"
    d.on_entry_outcome(zero_fill(third, AT))
    assert run(d, AT + timedelta(seconds=1)).decision.reason_code == R.RETRY_ATTEMPTS_EXHAUSTED
    off = driver()
    off.restore_window_usage(episodes=1, attempts=1, posts=1, filled_episodes=0,
                             latest_attempts=1, latest_state="NO_FILL")
    assert run(off, AT).decision.reason_code == R.EPISODE_CONSUMED


def test_model_readiness_does_not_depend_on_entry_permission() -> None:
    # The live host only allows entries once the model is ready: reporting "not ready"
    # while entries are blocked would deadlock the run (found in the first live run).
    blocked = run(driver(), AT, ctx_kwargs={"allowed": False})
    assert blocked.decision.reason_code == R.ENTRY_NOT_ALLOWED and not blocked.intents
    assert blocked.eligibility.model_ready is True
    not_ready = run(driver(), AT, ctx_kwargs={"allowed": False},
                    features=__import__("test_settlement_value").vol(ready=False))
    assert not_ready.eligibility.model_ready is False


# -- managed exits ---------------------------------------------------------------------

MANAGED = {
    "exit": {"policy": "managed_exit", "thesis_stop_enabled": True,
             "thesis_stop_probability": 0.99, "exit_cutoff_tau_s": 20},
}


def _filled(d, intent, at, shares="10"):
    d.on_entry_outcome(
        EntryOutcome(EntryOutcomeKind.FILLED, intent.intent_id.value, at, posted=True,
                     confirmed_shares=Decimal(shares), cost=Decimal(shares) * Decimal("0.3"))
    )


def test_hold_policy_rejects_strategy_exits() -> None:
    with pytest.raises(ConfigError, match="managed_exit"):
        settlement_value_config_from_parameters({"exit": {"thesis_stop_enabled": True}})
    with pytest.raises(ConfigError, match="managed_exit"):
        settlement_value_config_from_parameters({"reentry": {"max_trades_per_window": 2}})


def test_managed_exit_thesis_stop_sells_and_never_commits_first() -> None:
    d = driver(**MANAGED)
    (intent,) = run(d, AT).intents
    _filled(d, intent, AT)
    result = run(d, AT + timedelta(seconds=1), ctx_kwargs={"qty": "10"})
    (exit_intent,) = result.intents
    assert isinstance(exit_intent, ExitIntent) and exit_intent.reason_code == R.THESIS_STOP
    assert result.decision.action is StrategyAction.EXIT
    pending = run(d, AT + timedelta(seconds=2), ctx_kwargs={"qty": "10"})
    assert pending.decision.reason_code == R.EXIT_PENDING and not pending.intents


def test_managed_exit_residual_is_held_at_the_exit_cutoff() -> None:
    d = driver(exit={"policy": "managed_exit", "exit_cutoff_tau_s": 60})
    (intent,) = run(d, AT).intents
    _filled(d, intent, AT)
    managing = run(d, AT + timedelta(seconds=1), ctx_kwargs={"qty": "10"})
    assert managing.decision.reason_code == R.MANAGING_POSITION and not managing.intents
    late = run(d, START + timedelta(seconds=250), ctx_kwargs={"qty": "10"})  # tau = 50
    assert [i.reason_code for i in late.intents] == [R.EXIT_CUTOFF_RESIDUAL_HOLD]


def test_value_exit_sells_only_when_the_bid_beats_hold_value() -> None:
    d = driver(exit={"policy": "managed_exit", "value_exit_enabled": True,
                     "value_exit_margin_per_share": "0.02", "exit_cutoff_tau_s": 20})
    (intent,) = run(d, AT).intents
    _filled(d, intent, AT)
    cheap = run(d, AT + timedelta(seconds=1), ctx_kwargs={"qty": "10"})
    assert cheap.decision.reason_code == R.MANAGING_POSITION  # bid 0.29 < value ~0.5
    rich = run(d, AT + timedelta(seconds=2), ctx_kwargs={"qty": "10"}, up_ask="0.99")
    (sell,) = rich.intents
    assert isinstance(sell, ExitIntent) and sell.reason_code == R.VALUE_EXIT
    assert sell.min_price == Decimal("0.98")


# -- re-entry --------------------------------------------------------------------------

REENTRY = {
    **MANAGED,
    "reentry": {"max_trades_per_window": 2, "rearm": "cooldown", "cooldown_s": 5,
                "lock_after_thesis_stop": False, "last_entry_tau_s": 30},
}


def _close_by_sale(d, at):
    exit_id = d.state.exit_intent_id
    d.on_exit_outcome(ExitOutcome(ExitOutcomeKind.FLAT, exit_id, at, sold_shares=Decimal("10")))
    return run(d, at, ctx_kwargs={"qty": "0"})


def test_reentry_after_confirmed_closure_with_cooldown_and_cap() -> None:
    d = driver(**REENTRY)
    (intent,) = run(d, AT).intents
    _filled(d, intent, AT)
    run(d, AT + timedelta(seconds=1), ctx_kwargs={"qty": "10"})  # thesis stop exit
    closed = _close_by_sale(d, AT + timedelta(seconds=2))
    assert closed.decision.reason_code == R.REENTRY_COOLDOWN and not closed.intents
    (second,) = run(d, AT + timedelta(seconds=8)).intents
    assert second.evidence["episode_seq"] == 2 and second.evidence["attempt_id"] == "e2a1"
    _filled(d, second, AT + timedelta(seconds=8))
    run(d, AT + timedelta(seconds=9), ctx_kwargs={"qty": "10"})
    done = _close_by_sale(d, AT + timedelta(seconds=10))
    assert done.decision.reason_code == R.MAX_TRADES_PER_WINDOW and not done.intents


def test_protective_stop_locks_the_window() -> None:
    from dataclasses import replace

    from test_settlement_value import context, snapshot, vol

    params = {**REENTRY, "reentry": {**REENTRY["reentry"], "lock_after_protective_stop": True}}
    d = driver(**{**params, "exit": {"policy": "managed_exit", "exit_cutoff_tau_s": 20}})
    (intent,) = run(d, AT).intents
    _filled(d, intent, AT)
    managing = run(d, AT + timedelta(seconds=1), ctx_kwargs={"qty": "10"})
    assert managing.decision.reason_code == R.MANAGING_POSITION
    # The host's protective stop sold the position; the context reports why.
    snap = snapshot(AT + timedelta(seconds=9))
    ctx = replace(context(snap), last_exit_reason="PROTECTION_SL")
    locked = d.evaluate(market_snapshot=snap, causation_id=None,
                        correlation_id=snap.correlation_id, context=ctx, trigger="t",
                        features=vol())
    assert locked.decision.reason_code == R.WINDOW_LOCKED_AFTER_STOP and not locked.intents
    assert d.state.last_close_reason == "PROTECTION_SL"


def test_signal_reset_rearm_requires_the_side_to_become_ineligible() -> None:
    params = {**REENTRY, "reentry": {**REENTRY["reentry"], "rearm": "signal_reset",
                                     "cooldown_s": 0}}
    d = driver(**params)
    (intent,) = run(d, AT).intents
    _filled(d, intent, AT)
    run(d, AT + timedelta(seconds=1), ctx_kwargs={"qty": "10"})
    waiting = _close_by_sale(d, AT + timedelta(seconds=2))
    assert waiting.decision.reason_code == R.REENTRY_AWAITING_SIGNAL_RESET
    reset = run(d, AT + timedelta(seconds=3), up_ask="0.80", down_ask="0.80")
    assert not reset.intents and d.state.rearm_reset_seen
    (again,) = run(d, AT + timedelta(seconds=4)).intents
    assert again.evidence["episode_seq"] == 2
