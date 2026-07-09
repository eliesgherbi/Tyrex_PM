"""Unit tests for market-aware strategy lifecycle (Phase 1 Wave A / M0)."""

from __future__ import annotations

from tyrex_pm.runtime.strategy_lifecycle import (
    ENTRY_BLOCK_MIN_SURVIVAL_WINDOW,
    ENTRY_BLOCK_PHASE,
    MarketLifecycleGuard,
    StrategyRuntimePolicy,
    clock_is_known,
    parse_strategy_lifecycle_config,
)
from tyrex_pm.strategies.paired_binary.market_timing import (
    MARKET_TIMING_ACTIVE,
    MARKET_TIMING_CLOSED,
    MARKET_TIMING_NEAR_CLOSE,
    MARKET_TIMING_UNKNOWN,
    MarketTimingSnapshot,
)


def _policy(**over) -> StrategyRuntimePolicy:
    base = parse_strategy_lifecycle_config(
        {
            "mode": "market_aware",
            "exit_clock_source": "event_end_ts",
            "max_runtime_s": None,
            "fallback_max_runtime_s": 900,
            "flatten_before_event_end_s": 20,
            "block_new_entry_phases": ["near_close", "closed"],
            "min_survival_window_s": 45,
        }
    )
    return StrategyRuntimePolicy(**{**base.__dict__, **over})


def _snap(
    *,
    phase: str = MARKET_TIMING_ACTIVE,
    event_end_ts: float | None = 2000.0,
    event_start_ts: float | None = 1000.0,
    seconds_to_close: float | None = 300.0,
    now_ts: float = 1700.0,
) -> MarketTimingSnapshot:
    return MarketTimingSnapshot(
        market_id="m1",
        condition_id="c1",
        yes_token_id="y",
        no_token_id="n",
        event_start_ts=event_start_ts,
        event_end_ts=event_end_ts,
        now_ts=now_ts,
        phase=phase,
        seconds_to_start=(event_start_ts - now_ts) if event_start_ts is not None else None,
        seconds_to_close=seconds_to_close,
        near_close_window_s=45.0,
        source="scenario_metadata",
    )


def test_parse_absent_config_disabled() -> None:
    policy = parse_strategy_lifecycle_config(None)
    assert policy.enabled is False


def test_known_clock_suppresses_strategy_max_runtime_tick_cap() -> None:
    policy = _policy()
    guard = MarketLifecycleGuard(policy, loop_started_mono=0.0)
    snap = _snap()
    assert clock_is_known(snap)
    assert guard.suppress_strategy_max_runtime_tick_cap(snap) is True


def test_unknown_clock_uses_fallback_runtime() -> None:
    policy = _policy()
    guard = MarketLifecycleGuard(policy, loop_started_mono=0.0)
    snap = _snap(phase=MARKET_TIMING_UNKNOWN, event_end_ts=None, event_start_ts=None, seconds_to_close=None)
    assert guard.uses_fallback_runtime(snap) is True
    assert guard.is_fallback_runtime_exhausted(snap, now_mono=899.0) is False
    assert guard.is_fallback_runtime_exhausted(snap, now_mono=901.0) is True


def test_pre_close_flatten_required() -> None:
    policy = _policy(flatten_before_event_end_s=20.0)
    guard = MarketLifecycleGuard(policy, loop_started_mono=0.0)
    snap = _snap(phase=MARKET_TIMING_NEAR_CLOSE, seconds_to_close=15.0)
    decision = guard.should_pre_close_flatten(snap, has_open_exposure=True)
    assert decision.required is True
    assert decision.reason == "pre_close_window"


def test_closed_market_pre_close_flatten() -> None:
    policy = _policy()
    guard = MarketLifecycleGuard(policy, loop_started_mono=0.0)
    snap = _snap(phase=MARKET_TIMING_CLOSED, seconds_to_close=-1.0)
    decision = guard.should_pre_close_flatten(snap, has_open_exposure=True)
    assert decision.required is True
    assert decision.reason == "market_closed"


def test_entry_blocked_near_close() -> None:
    policy = _policy()
    guard = MarketLifecycleGuard(policy, loop_started_mono=0.0)
    snap = _snap(phase=MARKET_TIMING_NEAR_CLOSE, seconds_to_close=30.0)
    block = guard.should_block_new_entry(snap)
    assert block.blocked is True
    assert block.reason == ENTRY_BLOCK_PHASE


def test_entry_blocked_min_survival_window() -> None:
    policy = _policy(min_survival_window_s=45.0)
    guard = MarketLifecycleGuard(policy, loop_started_mono=0.0)
    snap = _snap(seconds_to_close=30.0)
    block = guard.should_block_new_entry(snap)
    assert block.blocked is True
    assert block.reason == ENTRY_BLOCK_MIN_SURVIVAL_WINDOW


def test_lifecycle_disabled_does_not_block_entry() -> None:
    policy = _policy(enabled=False)
    guard = MarketLifecycleGuard(policy, loop_started_mono=0.0)
    snap = _snap(phase=MARKET_TIMING_NEAR_CLOSE)
    block = guard.should_block_new_entry(snap)
    assert block.blocked is False


def test_continue_loop_market_clock_active_with_exposure() -> None:
    policy = _policy()
    guard = MarketLifecycleGuard(policy, loop_started_mono=0.0)
    snap = _snap()
    decision = guard.should_continue_loop(
        snapshot=snap,
        has_open_exposure=True,
        strategy_terminal=False,
        operator_stop=False,
    )
    assert decision.continue_loop is True
    assert decision.reason == "market_clock_active"
