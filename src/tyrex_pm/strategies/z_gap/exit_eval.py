"""Z-Gap exit trigger evaluation with deterministic precedence (A0.7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from tyrex_pm.exit_policy.thesis_stop import (
    EXIT_REASON_THESIS_STOP,
    ThesisStopDecision,
    ThesisStopTracker,
    evaluate_thesis_stop,
)
from tyrex_pm.quant.binary_fair_value import FairValueSnapshot
from tyrex_pm.quant.volatility import VolatilitySnapshot
from tyrex_pm.runtime.config import ZGapExitConfig
from tyrex_pm.runtime.time_authority import TimeAuthority
from tyrex_pm.runtime.z_gap_run import compute_tau_s
from tyrex_pm.strategies.z_gap.state import ZGapLifecycleState, ZGapPhase

EXIT_REASON_KILL_SWITCH = "kill_switch"
EXIT_REASON_LIFECYCLE_FLATTEN = "lifecycle_flatten"


@dataclass(frozen=True)
class ExitTriggerDecision:
    should_exit: bool
    exit_reason: str | None = None
    kill_reason: str | None = None
    held_leg: str | None = None
    current_z: Decimal | None = None
    z_stop: Decimal | None = None
    confirmation_duration_s: float | None = None
    tau_s: float | None = None


@dataclass
class ZGapExitEvalState:
    thesis_tracker: ThesisStopTracker = field(default_factory=ThesisStopTracker)
    lifecycle_flatten_triggered: bool = False
    kill_switch_triggered: bool = False


def evaluate_kill_switch(
    *,
    lifecycle: ZGapLifecycleState,
    manual_intervention: bool,
    market_data_critical: bool,
    lifecycle_failure_requires_flatten: bool,
    max_position_loss_breached: bool,
    eval_state: ZGapExitEvalState,
) -> ExitTriggerDecision | None:
    if eval_state.kill_switch_triggered:
        return None
    reasons: list[str] = []
    if manual_intervention:
        reasons.append("manual_intervention")
    if market_data_critical:
        reasons.append("market_data_quality_critical")
    if lifecycle_failure_requires_flatten:
        reasons.append("lifecycle_failure")
    if max_position_loss_breached:
        reasons.append("max_position_loss")
    if not reasons:
        return None
    eval_state.kill_switch_triggered = True
    return ExitTriggerDecision(
        should_exit=True,
        exit_reason=EXIT_REASON_KILL_SWITCH,
        kill_reason=",".join(reasons),
        held_leg=lifecycle.selected_leg,
    )


def evaluate_lifecycle_flatten(
    *,
    lifecycle: ZGapLifecycleState,
    event_end_ts: float | None,
    time_authority: TimeAuthority | None,
    now_ts: float,
    exit_cfg: ZGapExitConfig,
    eval_state: ZGapExitEvalState,
) -> ExitTriggerDecision | None:
    if eval_state.lifecycle_flatten_triggered:
        return None
    tau_s = compute_tau_s(event_end_ts, now_ts)
    if tau_s is None:
        return None
    if tau_s > exit_cfg.flatten_before_event_end_s:
        return None
    eval_state.lifecycle_flatten_triggered = True
    return ExitTriggerDecision(
        should_exit=True,
        exit_reason=EXIT_REASON_LIFECYCLE_FLATTEN,
        held_leg=lifecycle.selected_leg,
        tau_s=tau_s,
    )


def evaluate_exit_triggers(
    *,
    lifecycle: ZGapLifecycleState,
    fair: FairValueSnapshot,
    vol: VolatilitySnapshot,
    exit_cfg: ZGapExitConfig,
    eval_state: ZGapExitEvalState,
    event_end_ts: float | None,
    time_authority: TimeAuthority | None,
    now_ts: float,
    feeds_fresh: bool,
    manual_intervention: bool = False,
    market_data_critical: bool = False,
    lifecycle_failure_requires_flatten: bool = False,
    max_position_loss_breached: bool = False,
) -> ExitTriggerDecision:
    """Precedence: kill switch > thesis stop > lifecycle flatten."""
    if lifecycle.phase != ZGapPhase.ACTIVE:
        return ExitTriggerDecision(should_exit=False)

    kill = evaluate_kill_switch(
        lifecycle=lifecycle,
        manual_intervention=manual_intervention,
        market_data_critical=market_data_critical,
        lifecycle_failure_requires_flatten=lifecycle_failure_requires_flatten,
        max_position_loss_breached=max_position_loss_breached,
        eval_state=eval_state,
    )
    if kill is not None:
        return kill

    thesis: ThesisStopDecision = evaluate_thesis_stop(
        held_leg=lifecycle.selected_leg,
        fair=fair,
        vol=vol,
        exit_cfg=exit_cfg,
        tracker=eval_state.thesis_tracker,
        now_ts=now_ts,
        feeds_fresh=feeds_fresh,
    )
    if thesis.should_exit:
        return ExitTriggerDecision(
            should_exit=True,
            exit_reason=EXIT_REASON_THESIS_STOP,
            held_leg=thesis.held_leg,
            current_z=thesis.current_z,
            z_stop=thesis.z_stop,
            confirmation_duration_s=thesis.confirmation_duration_s,
        )

    flatten = evaluate_lifecycle_flatten(
        lifecycle=lifecycle,
        event_end_ts=event_end_ts,
        time_authority=time_authority,
        now_ts=now_ts,
        exit_cfg=exit_cfg,
        eval_state=eval_state,
    )
    if flatten is not None:
        return flatten

    return ExitTriggerDecision(should_exit=False)
