"""Lifecycle flatten tests (A0.7)."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.runtime.config import ZGapExitConfig
from tyrex_pm.runtime.time_authority import TimeAuthority
from tyrex_pm.strategies.z_gap.exit_eval import ZGapExitEvalState, evaluate_lifecycle_flatten
from tyrex_pm.strategies.z_gap.state import ZGapLifecycleState, ZGapPhase

EXIT = ZGapExitConfig(
    z_stop=Decimal("0.25"),
    stop_confirm_s=1.0,
    flatten_before_event_end_s=20,
    retry_interval_ms=1000,
    max_exit_attempts=5,
)

def _lc() -> ZGapLifecycleState:
    lc = ZGapLifecycleState(market_id="m1", condition_id="0xabc", owner_id="z_gap")
    lc.phase = ZGapPhase.ACTIVE
    lc.selected_leg = "UP"
    return lc


def test_triggers_at_configured_tau() -> None:
    lc = _lc()
    ev = ZGapExitEvalState()
    d = evaluate_lifecycle_flatten(
        lifecycle=lc,
        event_end_ts=1000.0,
        time_authority=TimeAuthority(sync_status="synced", samples_requested=1, samples_kept=1),
        now_ts=990.0,
        exit_cfg=EXIT,
        eval_state=ev,
    )
    assert d is not None
    assert d.should_exit
    assert d.exit_reason == "lifecycle_flatten"


def test_does_not_trigger_before_threshold() -> None:
    lc = _lc()
    ev = ZGapExitEvalState()
    d = evaluate_lifecycle_flatten(
        lifecycle=lc,
        event_end_ts=1000.0,
        time_authority=None,
        now_ts=970.0,
        exit_cfg=EXIT,
        eval_state=ev,
    )
    assert d is None


def test_trigger_once() -> None:
    lc = _lc()
    ev = ZGapExitEvalState()
    evaluate_lifecycle_flatten(
        lifecycle=lc,
        event_end_ts=1000.0,
        time_authority=None,
        now_ts=990.0,
        exit_cfg=EXIT,
        eval_state=ev,
    )
    d2 = evaluate_lifecycle_flatten(
        lifecycle=lc,
        event_end_ts=1000.0,
        time_authority=None,
        now_ts=991.0,
        exit_cfg=EXIT,
        eval_state=ev,
    )
    assert d2 is None
