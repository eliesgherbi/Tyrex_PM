"""Thesis-stop tests (A0.7)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from decimal import Decimal

from tyrex_pm.exit_policy.thesis_stop import ThesisStopTracker, evaluate_thesis_stop
from tyrex_pm.quant.binary_fair_value import FairValueSnapshot, MODEL_STATUS_READY
from tyrex_pm.quant.edge import LEG_DOWN, LEG_UP
from tyrex_pm.quant.volatility import VolatilitySnapshot
from tyrex_pm.runtime.config import ZGapExitConfig

TS = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
EXIT = ZGapExitConfig(
    z_stop=Decimal("0.25"),
    stop_confirm_s=1.0,
    flatten_before_event_end_s=20,
    retry_interval_ms=1000,
    max_exit_attempts=5,
)


def _fair(z: float, *, status: str = MODEL_STATUS_READY) -> FairValueSnapshot:
    return FairValueSnapshot(
        S=Decimal("100"),
        K=Decimal("100"),
        tau_s=120.0,
        sigma=0.02,
        sigma_units="per_sqrt_s",
        z=z,
        p_up=0.6,
        p_down=0.4,
        model_status=status,
        reject_reason=None,
        snapshot_ts=TS,
    )


def _vol(**over) -> VolatilitySnapshot:
    base = dict(
        sigma=0.00015,
        sigma_units="per_sqrt_second",
        ready=True,
        sample_count=30,
        effective_samples_s=25.0,
        last_update_ts=TS,
        jump_guard_tripped=False,
        reject_reason=None,
    )
    base.update(over)
    return VolatilitySnapshot(**base)


def test_up_exits_on_negative_z() -> None:
    tracker = ThesisStopTracker()
    d = evaluate_thesis_stop(
        held_leg=LEG_UP,
        fair=_fair(-0.3),
        vol=_vol(),
        exit_cfg=EXIT,
        tracker=tracker,
        now_ts=100.0,
        feeds_fresh=True,
    )
    assert not d.should_exit
    d2 = evaluate_thesis_stop(
        held_leg=LEG_UP,
        fair=_fair(-0.3),
        vol=_vol(),
        exit_cfg=EXIT,
        tracker=tracker,
        now_ts=101.5,
        feeds_fresh=True,
    )
    assert d2.should_exit


def test_down_exits_on_positive_z() -> None:
    tracker = ThesisStopTracker()
    evaluate_thesis_stop(
        held_leg=LEG_DOWN,
        fair=_fair(0.3),
        vol=_vol(),
        exit_cfg=EXIT,
        tracker=tracker,
        now_ts=10.0,
        feeds_fresh=True,
    )
    d = evaluate_thesis_stop(
        held_leg=LEG_DOWN,
        fair=_fair(0.3),
        vol=_vol(),
        exit_cfg=EXIT,
        tracker=tracker,
        now_ts=11.5,
        feeds_fresh=True,
    )
    assert d.should_exit


def test_no_stop_same_side_z() -> None:
    tracker = ThesisStopTracker()
    d = evaluate_thesis_stop(
        held_leg=LEG_UP,
        fair=_fair(1.0),
        vol=_vol(),
        exit_cfg=EXIT,
        tracker=tracker,
        now_ts=100.0,
        feeds_fresh=True,
    )
    assert not d.should_exit


def test_stale_model_no_trigger() -> None:
    tracker = ThesisStopTracker()
    d = evaluate_thesis_stop(
        held_leg=LEG_UP,
        fair=_fair(-1.0, status="not_ready"),
        vol=_vol(ready=False),
        exit_cfg=EXIT,
        tracker=tracker,
        now_ts=100.0,
        feeds_fresh=True,
    )
    assert not d.should_exit


def test_confirmation_respected() -> None:
    tracker = ThesisStopTracker()
    evaluate_thesis_stop(
        held_leg=LEG_UP,
        fair=_fair(-0.5),
        vol=_vol(),
        exit_cfg=EXIT,
        tracker=tracker,
        now_ts=0.0,
        feeds_fresh=True,
    )
    d = evaluate_thesis_stop(
        held_leg=LEG_UP,
        fair=_fair(-0.5),
        vol=_vol(),
        exit_cfg=EXIT,
        tracker=tracker,
        now_ts=0.5,
        feeds_fresh=True,
    )
    assert not d.should_exit


def test_trigger_once() -> None:
    tracker = ThesisStopTracker()
    evaluate_thesis_stop(
        held_leg=LEG_UP,
        fair=_fair(-0.5),
        vol=_vol(),
        exit_cfg=EXIT,
        tracker=tracker,
        now_ts=0.0,
        feeds_fresh=True,
    )
    d1 = evaluate_thesis_stop(
        held_leg=LEG_UP,
        fair=_fair(-0.5),
        vol=_vol(),
        exit_cfg=EXIT,
        tracker=tracker,
        now_ts=2.0,
        feeds_fresh=True,
    )
    d2 = evaluate_thesis_stop(
        held_leg=LEG_UP,
        fair=_fair(-0.9),
        vol=_vol(),
        exit_cfg=EXIT,
        tracker=tracker,
        now_ts=3.0,
        feeds_fresh=True,
    )
    assert d1.should_exit
    assert not d2.should_exit
