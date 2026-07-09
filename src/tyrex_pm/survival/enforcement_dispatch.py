"""Survival enforce-mode exit dispatch (Phase 1 operational layer)."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Literal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import RunId
from tyrex_pm.core.models import CancelIntent, URGENCY_NORMAL, URGENCY_URGENT
from tyrex_pm.market_data.quality import DecisionContext
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import AppConfig, PairedBinaryStrategyConfig, PairedBinaryStrategyConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.intent_work import IntentWorkUnit
from tyrex_pm.strategies.paired_binary.entry_eval import LegBook
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from tyrex_pm.survival.context import downgrade_survivor_target_one_step
from tyrex_pm.survival.exit_planning import SurvivalExitPlanner
from tyrex_pm.survival.models import SurvivalAdvisoryResult, SurvivorTargetMode
from tyrex_pm.survival.order_policy import (
    SurvivalExitOrderDecision,
    SurvivalRestingState,
    is_retryable_fak_reject,
    order_decision_payload,
    select_survival_exit_order,
)

SurvivalEnforceTrigger = Literal[
    "survival_trailing_stop",
    "survival_stall_exit",
    "survival_economics_exit",
    "survival_hard_floor",
]

_PENDING_PHASES = {
    PairedBinaryPhase.STOP_PENDING_YES,
    PairedBinaryPhase.STOP_PENDING_NO,
    PairedBinaryPhase.TP_PENDING_YES,
    PairedBinaryPhase.TP_PENDING_NO,
    PairedBinaryPhase.TIMEOUT_PENDING,
    PairedBinaryPhase.EXITING_YES,
    PairedBinaryPhase.EXITING_NO,
    PairedBinaryPhase.EXITING_BOTH,
}

_ACTIVE_RESTING = frozenset(
    {
        SurvivalRestingState.RESTING_PLACED.value,
        SurvivalRestingState.RESTING_PARTIAL_FILL.value,
        SurvivalRestingState.RESTING_CANCEL_REQUESTED.value,
        SurvivalRestingState.RESTING_REPLACE_SCHEDULED.value,
    }
)


def _survival_raw(state: PairedBinaryRuntimeState) -> dict[str, Any]:
    if state.survivor_leg_state is None:
        state.survivor_leg_state = {}
    return state.survivor_leg_state


def survival_attempt_count(state: PairedBinaryRuntimeState) -> int:
    return int(_survival_raw(state).get("survival_exit_attempt_count", 0))


def survival_fak_reject_count(state: PairedBinaryRuntimeState) -> int:
    return int(_survival_raw(state).get("survival_fak_reject_count", 0))


def resting_order_open(state: PairedBinaryRuntimeState) -> bool:
    raw = _survival_raw(state)
    oid = raw.get("survival_resting_order_id")
    st = raw.get("survival_resting_state")
    return bool(oid and st in _ACTIVE_RESTING)


def increment_survival_exit_attempt(state: PairedBinaryRuntimeState) -> int:
    raw = _survival_raw(state)
    n = int(raw.get("survival_exit_attempt_count", 0)) + 1
    raw["survival_exit_attempt_count"] = n
    return n


def record_survival_fak_reject(state: PairedBinaryRuntimeState) -> int:
    raw = _survival_raw(state)
    n = int(raw.get("survival_fak_reject_count", 0)) + 1
    raw["survival_fak_reject_count"] = n
    return n


def clear_enforce_in_flight(state: PairedBinaryRuntimeState) -> None:
    raw = _survival_raw(state)
    raw["enforce_exit_in_flight"] = False
    raw["enforce_exit_pending"] = False


def mark_enforce_in_flight(state: PairedBinaryRuntimeState, *, module: str) -> None:
    raw = _survival_raw(state)
    raw["enforce_exit_in_flight"] = True
    raw["enforce_exit_pending"] = True
    raw["enforce_module"] = module


def mark_resting_order_placed(
    state: PairedBinaryRuntimeState,
    *,
    order_id: str,
    local_ttl_s: float | None,
) -> None:
    raw = _survival_raw(state)
    raw["survival_resting_order_id"] = order_id
    raw["survival_resting_state"] = SurvivalRestingState.RESTING_PLACED.value
    raw["survival_resting_placed_ts"] = time.time()
    raw["survival_resting_local_ttl_s"] = local_ttl_s
    clear_enforce_in_flight(state)


def mark_resting_cancelled(state: PairedBinaryRuntimeState) -> None:
    raw = _survival_raw(state)
    raw["survival_resting_state"] = SurvivalRestingState.RESTING_CANCELLED.value
    raw["survival_resting_order_id"] = None


def mark_resting_abandoned(state: PairedBinaryRuntimeState, *, reason: str) -> None:
    raw = _survival_raw(state)
    raw["survival_resting_state"] = SurvivalRestingState.RESTING_ABANDONED.value
    raw["survival_resting_abandon_reason"] = reason


def resting_ttl_expired(state: PairedBinaryRuntimeState, *, now: float | None = None) -> bool:
    raw = _survival_raw(state)
    placed = raw.get("survival_resting_placed_ts")
    ttl = raw.get("survival_resting_local_ttl_s")
    if placed is None or ttl is None:
        return False
    ts = now if now is not None else time.time()
    return (ts - float(placed)) >= float(ttl)


def build_survival_order_extensions(
    *,
    module: str,
    trigger_type: str,
    decision: SurvivalExitOrderDecision,
    survivor_leg: str,
) -> dict[str, Any]:
    return {
        "survival_exit": True,
        "survival_module": module,
        "survival_trigger_type": trigger_type,
        "survival_order_type": decision.order_type.value,
        "survival_policy_mode": decision.policy_mode,
        "survival_order_attempt": decision.attempt_count,
        "survival_order_reason": decision.reason,
        "survival_local_ttl_s": decision.local_ttl_s,
    }


def handle_survival_exit_oms_reject(
    state: PairedBinaryRuntimeState,
    *,
    leg: str,
    trigger_type: str,
    error_msg: str | None,
) -> bool:
    """Record retryable FAK reject; returns True if retry should be scheduled."""
    from tyrex_pm.strategies.paired_binary.exit_engine import record_exit_blocked

    clear_enforce_in_flight(state)
    if not is_retryable_fak_reject(error_msg):
        return False
    record_survival_fak_reject(state)
    increment_survival_exit_attempt(state)
    record_exit_blocked(state, leg, reason="OMS_REJECTED", trigger_type=trigger_type)  # type: ignore[arg-type]
    return True


def select_enforce_order_decision(
    app: AppConfig,
    state: PairedBinaryRuntimeState,
    *,
    survivor_leg: str,
    qty: Decimal,
    touch_price: Decimal,
    executable_price: Decimal | None,
    seconds_to_close: float | None,
    allow_partial: bool,
    trigger_type: str,
    module: str,
    is_retry: bool,
) -> SurvivalExitOrderDecision | None:
    policy = app.survival.enforcement.order_policy
    attempt = survival_attempt_count(state)
    if not is_retry:
        attempt = 0
    fak_rejects = survival_fak_reject_count(state) if is_retry else 0
    side = Side.SELL if survivor_leg in {"yes", "no"} else Side.SELL
    return select_survival_exit_order(
        cfg=policy,
        side=side,
        qty=qty,
        touch_price=touch_price,
        executable_price=executable_price,
        attempt_count=attempt,
        fak_reject_count=fak_rejects,
        seconds_to_close=seconds_to_close,
        allow_partial=allow_partial,
        resting_order_open=resting_order_open(state),
        trigger_type=trigger_type,
        module=module,
    )


def build_resting_cancel_work(
    state: PairedBinaryRuntimeState,
    *,
    token_id: str,
    leg: str,
    pair_id: str,
    venue_order_id: str,
) -> IntentWorkUnit | None:
    leg_rt = state.yes if leg == "yes" else state.no
    leg_corr = leg_rt.leg_correlation_id or f"{pair_id}:{leg}:survival_cancel"
    raw = _survival_raw(state)
    raw["survival_resting_state"] = SurvivalRestingState.RESTING_CANCEL_REQUESTED.value
    return IntentWorkUnit(
        intent=CancelIntent(venue_order_id=venue_order_id, client_order_id=None),
        correlation_id=leg_corr,
        intent_fact_extensions={
            "source": "survival_resting_cancel",
            "leg": leg,
            "token_id": token_id,
            "survival_exit": True,
            "survival_resting_cancel": True,
            "cancel_order_id": venue_order_id,
        },
    )


def build_survival_shutdown_resting_cancel(
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
    *,
    reason: str = "terminal_shutdown",
) -> IntentWorkUnit | None:
    """Cancel open survival resting exit before shutdown flatten."""
    if not resting_order_open(state):
        return None
    raw = _survival_raw(state)
    leg = survivor_leg_from_state(state)
    if leg is None:
        return None
    oid = raw.get("survival_resting_order_id")
    if not oid:
        return None
    token_id = cfg.yes_token_id if leg == "yes" else cfg.no_token_id
    pair_id = state.pair_correlation_id or "paired_binary_unknown"
    raw["survival_shutdown_cancel_reason"] = reason
    return build_resting_cancel_work(
        state,
        token_id=str(token_id),
        leg=str(leg),
        pair_id=pair_id,
        venue_order_id=str(oid),
    )


def enforce_trigger_from_reason(reason: str | None) -> SurvivalEnforceTrigger:
    if reason == "hard_floor_breach":
        return "survival_hard_floor"
    if reason and reason.startswith("stall_"):
        return "survival_stall_exit"
    if reason == "economics_early_exit":
        return "survival_economics_exit"
    return "survival_trailing_stop"


def enforce_module_from_result(result: SurvivalAdvisoryResult) -> str | None:
    if result.enforce_module:
        return result.enforce_module
    if result.enforce_exit:
        reason = result.enforce_exit_reason or ""
        if reason.startswith("stall_"):
            return "stall_exit"
        if reason == "hard_floor_breach":
            return "survivor_floor"
        if reason == "economics_early_exit":
            return "economics"
        return "trailing_stop"
    if result.enforce_stall_downgrade:
        return "stall_exit"
    return None


def survivor_leg_from_state(state: PairedBinaryRuntimeState) -> str | None:
    if state.phase == PairedBinaryPhase.ONLY_YES_ACTIVE:
        return "yes"
    if state.phase == PairedBinaryPhase.ONLY_NO_ACTIVE:
        return "no"
    return None


def _loser_leg(survivor_leg: str) -> str:
    return "no" if survivor_leg == "yes" else "yes"


def _loser_exit_fill(state: PairedBinaryRuntimeState, loser_leg: str) -> Decimal | None:
    rt = state.yes if loser_leg == "yes" else state.no
    if rt.exit_cash is not None and rt.exit_qty and rt.exit_qty > 0:
        return rt.exit_cash / rt.exit_qty
    return None


def should_skip_survival_enforce(
    state: PairedBinaryRuntimeState,
    *,
    survivor_leg: str,
    seconds_to_close: float | None,
    flatten_before_event_end_s: float,
) -> tuple[bool, str | None]:
    leg_rt = state.yes if survivor_leg == "yes" else state.no
    if leg_rt.exit_submitted:
        return True, "exit_already_submitted"
    raw = state.survivor_leg_state or {}
    if raw.get("enforce_exit_in_flight") or raw.get("enforce_exit_pending"):
        return True, "enforce_exit_in_flight"
    if resting_order_open(state):
        return True, "resting_order_open"
    if state.phase in _PENDING_PHASES and not raw.get("survival_fak_reject_count"):
        return True, "exit_already_pending"
    if seconds_to_close is not None and seconds_to_close <= flatten_before_event_end_s:
        return True, "pre_close_flatten_preempts"
    return False, None


def build_enforce_exit_payload(
    *,
    module: str,
    reason: str | None,
    survivor_leg: str,
    result: SurvivalAdvisoryResult,
    enforcement_mode: str,
    existing_exit_pending: bool,
    skip_reason: str | None = None,
    target_mode: str | None = None,
) -> dict[str, Any]:
    exit_eval = result.exit_eval
    ev = exit_eval.evidence if exit_eval else None
    payload: dict[str, Any] = {
        "module": module,
        "reason": reason,
        "survivor_leg": survivor_leg,
        "target_mode": target_mode,
        "enforcement_mode": enforcement_mode,
        "existing_exit_pending": existing_exit_pending,
    }
    if ev is not None and exit_eval is not None:
        payload.update(
            {
                "current_executable_bid": str(ev.executable_bid) if ev.executable_bid is not None else None,
                "touch_bid": str(ev.touch_bid) if ev.touch_bid is not None else None,
                "sweep_vwap": str(ev.sweep_vwap) if ev.sweep_vwap is not None else None,
                "depth_fraction": str(ev.available_depth_fraction),
                "recommended_qty": str(exit_eval.recommended_qty),
                "planner_evidence_ref": ev.planner_evidence_ref,
            }
        )
    if skip_reason:
        payload["skip_reason"] = skip_reason
    return payload


def apply_stall_enforce_downgrade(
    *,
    app: AppConfig,
    state: PairedBinaryRuntimeState,
    cfg: PairedBinaryStrategyConfig,
    survivor_leg: str,
    yes_book: LegBook,
    no_book: LegBook,
    sink: JsonlSink | None,
    run_id: RunId | None,
    seconds_to_close: float | None,
) -> bool:
    """Downgrade survivor target one step when stall enforce + downgrade action."""
    from tyrex_pm.strategies.paired_binary import facts as pb_facts

    raw = state.survivor_leg_state or {}
    current_mode = str(raw.get("selected_mode", SurvivorTargetMode.FULL_RECOVERY.value))
    if raw.get("last_stall_downgrade_from_mode") == current_mode:
        return False

    loser = _loser_leg(survivor_leg)
    fill = _loser_exit_fill(state, loser)
    if fill is None:
        return False
    survivor_bid = yes_book.bid if survivor_leg == "yes" else no_book.bid
    downgraded = downgrade_survivor_target_one_step(
        state,
        cfg,
        app.survival,
        survivor_leg=survivor_leg,
        loser_leg=loser,
        loser_exit_fill=fill,
        seconds_to_close=seconds_to_close,
        survivor_bid=survivor_bid,
    )
    if downgraded is None:
        return False
    from_mode, to_mode, _plan = downgraded
    if state.survivor_leg_state is None:
        state.survivor_leg_state = {}
    state.survivor_leg_state["last_stall_downgrade_from_mode"] = from_mode.value
    if sink and run_id:
        pb_facts.emit_survivor_target_downgraded(
            sink,
            run_id,
            state,
            yes_book,
            no_book,
            from_mode=from_mode.value,
            to_mode=to_mode.value,
            classification="stall_enforce_downgrade",
        )
        pb_facts.emit_survival_enforce_exit_requested(
            sink,
            run_id,
            state,
            yes_book,
            no_book,
            payload={
                "module": "stall_exit",
                "reason": "stall_downgrade",
                "survivor_leg": survivor_leg,
                "target_mode": to_mode.value,
                "enforcement_mode": app.survival.stall_exit.enforcement_mode,
                "existing_exit_pending": False,
                "action": "downgrade",
                "from_mode": from_mode.value,
                "to_mode": to_mode.value,
            },
        )
    return True


def mark_enforce_exit_pending(state: PairedBinaryRuntimeState, *, module: str) -> None:
    mark_enforce_in_flight(state, module=module)
    increment_survival_exit_attempt(state)
    clear_pending_survival_exit_intent(state)


def executable_bid_for_dispatch(
    result: SurvivalAdvisoryResult,
    planner: SurvivalExitPlanner | None,
) -> Decimal | None:
    if result.exit_eval is None:
        return None
    if planner is not None:
        bid = planner.executable_bid_for_progress(result.exit_eval)
        if bid is not None:
            return bid
    ev = result.exit_eval.evidence
    return ev.sweep_vwap or ev.executable_bid or ev.touch_bid


def enforce_decision_context(trigger: str) -> DecisionContext:
    return DecisionContext.URGENT_EXIT


_PENDING_INTENT_KEYS = (
    "pending_survival_exit_intent",
    "pending_survival_exit_reason",
    "pending_survival_exit_module",
    "pending_survival_exit_trigger_type",
    "pending_survival_exit_first_ts",
    "pending_survival_exit_last_attempt_ts",
    "pending_survival_exit_attempt_count",
    "pending_survival_exit_last_skip_reason",
    "pending_survival_exit_quality_reject_detail",
)


def has_pending_survival_exit_intent(state: PairedBinaryRuntimeState) -> bool:
    return bool(_survival_raw(state).get("pending_survival_exit_intent"))


def latch_pending_survival_exit_intent(
    state: PairedBinaryRuntimeState,
    *,
    module: str,
    reason: str | None,
    trigger_type: str,
    skip_reason: str,
    quality_reject_detail: dict[str, bool] | None,
) -> None:
    raw = _survival_raw(state)
    now = time.time()
    if not raw.get("pending_survival_exit_intent"):
        raw["pending_survival_exit_first_ts"] = now
        raw["pending_survival_exit_attempt_count"] = 0
    raw["pending_survival_exit_intent"] = True
    raw["pending_survival_exit_reason"] = reason
    raw["pending_survival_exit_module"] = module
    raw["pending_survival_exit_trigger_type"] = trigger_type
    raw["pending_survival_exit_last_skip_reason"] = skip_reason
    raw["pending_survival_exit_quality_reject_detail"] = quality_reject_detail
    raw["pending_survival_exit_last_attempt_ts"] = now


def clear_pending_survival_exit_intent(state: PairedBinaryRuntimeState) -> None:
    raw = _survival_raw(state)
    for key in _PENDING_INTENT_KEYS:
        raw.pop(key, None)


def pending_survival_exit_context(state: PairedBinaryRuntimeState) -> dict[str, Any]:
    raw = _survival_raw(state)
    return {
        "module": raw.get("pending_survival_exit_module"),
        "reason": raw.get("pending_survival_exit_reason"),
        "trigger_type": raw.get("pending_survival_exit_trigger_type"),
        "attempt_count": int(raw.get("pending_survival_exit_attempt_count", 0)),
        "first_ts": raw.get("pending_survival_exit_first_ts"),
        "last_attempt_ts": raw.get("pending_survival_exit_last_attempt_ts"),
        "last_skip_reason": raw.get("pending_survival_exit_last_skip_reason"),
        "quality_reject_detail": raw.get("pending_survival_exit_quality_reject_detail"),
    }


def record_pending_survival_exit_retry_attempt(state: PairedBinaryRuntimeState) -> int:
    raw = _survival_raw(state)
    now = time.time()
    n = int(raw.get("pending_survival_exit_attempt_count", 0)) + 1
    raw["pending_survival_exit_attempt_count"] = n
    raw["pending_survival_exit_last_attempt_ts"] = now
    return n


def should_retry_pending_survival_exit(
    state: PairedBinaryRuntimeState,
    cfg: object,
    *,
    now: float | None = None,
) -> tuple[bool, str | None]:
    if not has_pending_survival_exit_intent(state):
        return False, None
    if not getattr(cfg, "retry_quality_rejects", False):
        return False, "retry_disabled"
    ts = now if now is not None else time.time()
    raw = _survival_raw(state)
    last = float(raw.get("pending_survival_exit_last_attempt_ts", 0))
    if ts - last < float(getattr(cfg, "quality_reject_retry_backoff_s", 0.25)):
        return False, "backoff"
    return True, None


def abandon_reason_for_pending_survival_exit(
    state: PairedBinaryRuntimeState,
    cfg: object,
    *,
    now: float | None = None,
) -> str | None:
    if not has_pending_survival_exit_intent(state):
        return None
    ts = now if now is not None else time.time()
    raw = _survival_raw(state)
    first = float(raw.get("pending_survival_exit_first_ts", ts))
    if ts - first > float(getattr(cfg, "abandon_quality_reject_after_s", 10.0)):
        return "abandon_timeout"
    if int(raw.get("pending_survival_exit_attempt_count", 0)) >= int(
        getattr(cfg, "max_quality_reject_retries", 10)
    ):
        return "max_retries"
    return None


def abandon_pending_survival_exit_intent(
    state: PairedBinaryRuntimeState,
    *,
    reason: str,
) -> dict[str, Any]:
    ctx = pending_survival_exit_context(state)
    ctx["abandon_reason"] = reason
    clear_pending_survival_exit_intent(state)
    return ctx


def pending_intent_retry_payload(
    state: PairedBinaryRuntimeState,
    *,
    seconds_to_close: float | None,
    backoff_s: float,
) -> dict[str, Any]:
    raw = _survival_raw(state)
    last = float(raw.get("pending_survival_exit_last_attempt_ts", time.time()))
    return {
        "attempt_count": int(raw.get("pending_survival_exit_attempt_count", 0)),
        "next_retry_ts": last + backoff_s,
        "seconds_to_close": seconds_to_close,
    }
