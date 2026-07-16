"""Z-Gap enforce entry plan construction and pre-submit validation (A0.6)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import EnterIntent
from tyrex_pm.market_data.book_read import PairBookSnapshot
from tyrex_pm.quant.binary_fair_value import FairValueSnapshot
from tyrex_pm.quant.edge import LEG_DOWN, LEG_UP, EdgeSnapshot
from tyrex_pm.quant.entry_cap import (
    EntryCapError,
    max_fill_price_for_edge_floor,
    predicted_edge_at_limit,
)
from tyrex_pm.quant.fees import FEE_MODEL_STATUS_RESOLVED, FeeModel
from tyrex_pm.runtime.config import (
    Z_GAP_ENTRY_MODE_ENFORCE,
    Z_GAP_ENTRY_MODE_OBSERVE_ONLY,
    ZGapEntryConfig,
    ZGapSizingConfig,
)
from tyrex_pm.runtime.intent_work import IntentWorkUnit
from tyrex_pm.runtime.time_authority import DEFAULT_ENFORCE_UNCERTAINTY_MAX_MS, SYNC_STATUS_SYNCED, TimeAuthority
from tyrex_pm.runtime.z_gap_preflight import ZGapPreflightGates
from tyrex_pm.state.signal_state_store import (
    FRESHNESS_LATE,
    FRESHNESS_MISSING,
    FRESHNESS_OBSERVED,
    FRESHNESS_OBSERVED_FROM_LOG,
    FRESHNESS_PENDING,
    SignalSnapshot,
)
from tyrex_pm.strategies.z_gap.entry_eval import (
    DECISION_NOT_READY,
    DECISION_SKIP,
    DECISION_WOULD_ENTER,
    ZGapEntryEvaluation,
)
from tyrex_pm.strategies.z_gap.sizing import (
    REASON_MISSING_LIMIT_PRICE,
    REASON_NOTIONAL_CAP,
    REASON_SIZE_BELOW_MIN,
    REASON_VENUE_MIN_SIZE,
    ZGapSizingError,
    compute_fixed_usd_shares,
)

PLAN_STATUS_READY = "ready"
PLAN_STATUS_BLOCKED = "blocked"
PLAN_STATUS_NOT_READY = "not_ready"

ORDER_STYLE_FAK = "FAK"
TIME_IN_FORCE_FAK = "FAK"

REASON_MODEL_CAP = "z_gap_entry_blocked_model_cap"
REASON_SIZE_BELOW_MIN = "z_gap_entry_blocked_size_below_min"
REASON_NOTIONAL_CAP = "z_gap_entry_blocked_notional_cap"
REASON_MISSING_TOKEN = "z_gap_entry_blocked_missing_token"
REASON_FEE_MODEL_UNKNOWN = "z_gap_entry_blocked_fee_model_unknown"
REASON_EDGE_FLOOR = "z_gap_entry_blocked_edge_floor"
REASON_INVALID_TICK = "z_gap_entry_blocked_invalid_tick"
REASON_EVAL_SKIP = "z_gap_entry_blocked_eval_skip"
REASON_MISSING_SIZING = "z_gap_entry_blocked_missing_sizing"
REASON_LIMIT_ABOVE_ASK = "z_gap_entry_blocked_limit_above_ask"
REASON_OBSERVE_MODE = "z_gap_entry_blocked_observe_mode"
REASON_CLOCK_SYNC = "z_gap_entry_blocked_clock_sync"
REASON_PTB_UNUSABLE = "z_gap_entry_blocked_ptb_unusable"
REASON_CALIBRATION_NOT_REVIEWED = "z_gap_entry_blocked_calibration_not_reviewed"
REASON_OPERATOR_NOT_APPROVED = "z_gap_entry_blocked_operator_not_approved"
REASON_PREFLIGHT = "z_gap_entry_blocked_preflight"

Z_GAP_INTENT_SOURCE = "z_gap_entry"


@dataclass(frozen=True)
class ZGapEntryPlan:
    market_id: str
    condition_id: str | None
    selected_leg: str | None
    token_id: str | None
    p_L: Decimal | None
    ask_seen: Decimal | None
    theta_fill_floor: Decimal | None
    max_fill_price: Decimal | None
    final_limit_price: Decimal | None
    phi_at_limit: Decimal | None
    expected_slippage: Decimal | None
    predicted_edge_at_limit: Decimal | None
    shares: Decimal | None
    notional_usd: Decimal | None
    order_style: str
    time_in_force: str
    entry_mode: str
    plan_status: str
    reject_reason: str | None
    created_ts: datetime

    def to_fact_payload(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "condition_id": self.condition_id,
            "selected_leg": self.selected_leg,
            "token_id": self.token_id,
            "p_L": str(self.p_L) if self.p_L is not None else None,
            "ask_seen": str(self.ask_seen) if self.ask_seen is not None else None,
            "theta_fill_floor": str(self.theta_fill_floor) if self.theta_fill_floor is not None else None,
            "max_fill_price": str(self.max_fill_price) if self.max_fill_price is not None else None,
            "final_limit_price": str(self.final_limit_price) if self.final_limit_price is not None else None,
            "phi_at_limit": str(self.phi_at_limit) if self.phi_at_limit is not None else None,
            "expected_slippage": str(self.expected_slippage) if self.expected_slippage is not None else None,
            "predicted_edge_at_limit": (
                str(self.predicted_edge_at_limit) if self.predicted_edge_at_limit is not None else None
            ),
            "shares": str(self.shares) if self.shares is not None else None,
            "notional_usd": str(self.notional_usd) if self.notional_usd is not None else None,
            "order_style": self.order_style,
            "time_in_force": self.time_in_force,
            "plan_status": self.plan_status,
            "reject_reason": self.reject_reason,
            "entry_mode": self.entry_mode,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _blocked_plan(
    *,
    market_id: str,
    condition_id: str | None,
    entry_mode: str,
    entry_cfg: ZGapEntryConfig | None,
    reject_reason: str,
    selected_leg: str | None = None,
    token_id: str | None = None,
    p_L: Decimal | None = None,
    ask_seen: Decimal | None = None,
    expected_slippage: Decimal | None = None,
    plan_status: str = PLAN_STATUS_BLOCKED,
) -> ZGapEntryPlan:
    theta = entry_cfg.theta_fill_floor if entry_cfg is not None else None
    return ZGapEntryPlan(
        market_id=market_id,
        condition_id=condition_id,
        selected_leg=selected_leg,
        token_id=token_id,
        p_L=p_L,
        ask_seen=ask_seen,
        theta_fill_floor=theta,
        max_fill_price=None,
        final_limit_price=None,
        phi_at_limit=None,
        expected_slippage=expected_slippage,
        predicted_edge_at_limit=None,
        shares=None,
        notional_usd=None,
        order_style=ORDER_STYLE_FAK,
        time_in_force=TIME_IN_FORCE_FAK,
        entry_mode=entry_mode,
        plan_status=plan_status,
        reject_reason=reject_reason,
        created_ts=_utc_now(),
    )


def _leg_p_and_ask(
    *,
    selected_leg: str,
    fair: FairValueSnapshot,
    edge: EdgeSnapshot,
) -> tuple[Decimal | None, Decimal | None]:
    if selected_leg == LEG_UP:
        p_L = Decimal(str(fair.p_up)) if fair.p_up is not None else None
        return p_L, edge.ask_up
    if selected_leg == LEG_DOWN:
        p_L = Decimal(str(fair.p_down)) if fair.p_down is not None else None
        return p_L, edge.ask_down
    return None, None


def _token_for_leg(*, selected_leg: str, yes_token_id: str, no_token_id: str) -> str | None:
    if selected_leg == LEG_UP:
        return yes_token_id or None
    if selected_leg == LEG_DOWN:
        return no_token_id or None
    return None


def build_z_gap_entry_plan(
    *,
    evaln: ZGapEntryEvaluation,
    fair: FairValueSnapshot,
    edge: EdgeSnapshot,
    books: PairBookSnapshot,
    entry_cfg: ZGapEntryConfig,
    sizing_cfg: ZGapSizingConfig | None,
    fee_model: FeeModel | None,
    market_id: str,
    condition_id: str | None,
    yes_token_id: str,
    no_token_id: str,
    entry_mode: str,
    tick_size: Decimal,
    venue_min_order_size: Decimal | None = None,
) -> ZGapEntryPlan:
    """Convert entry evaluation into a model-capped FAK BUY plan (no submit)."""
    _ = books  # reserved for future depth participation caps (A0.7+)
    expected_slippage = Decimal(str(entry_cfg.expected_slippage_ticks)) * tick_size

    if evaln.decision_status == DECISION_NOT_READY:
        return _blocked_plan(
            market_id=market_id,
            condition_id=condition_id,
            entry_mode=entry_mode,
            entry_cfg=entry_cfg,
            reject_reason=evaln.reason_code or REASON_EVAL_SKIP,
            selected_leg=evaln.selected_leg,
            expected_slippage=expected_slippage,
            plan_status=PLAN_STATUS_NOT_READY,
        )

    if evaln.decision_status != DECISION_WOULD_ENTER:
        return _blocked_plan(
            market_id=market_id,
            condition_id=condition_id,
            entry_mode=entry_mode,
            entry_cfg=entry_cfg,
            reject_reason=evaln.reason_code or REASON_EVAL_SKIP,
            selected_leg=evaln.selected_leg,
            expected_slippage=expected_slippage,
        )

    selected_leg = evaln.selected_leg
    if selected_leg not in {LEG_UP, LEG_DOWN}:
        return _blocked_plan(
            market_id=market_id,
            condition_id=condition_id,
            entry_mode=entry_mode,
            entry_cfg=entry_cfg,
            reject_reason=REASON_MISSING_TOKEN,
            expected_slippage=expected_slippage,
        )

    token_id = _token_for_leg(
        selected_leg=selected_leg,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
    )
    if not token_id:
        return _blocked_plan(
            market_id=market_id,
            condition_id=condition_id,
            entry_mode=entry_mode,
            entry_cfg=entry_cfg,
            reject_reason=REASON_MISSING_TOKEN,
            selected_leg=selected_leg,
            expected_slippage=expected_slippage,
        )

    p_L, ask_seen = _leg_p_and_ask(selected_leg=selected_leg, fair=fair, edge=edge)
    if p_L is None or ask_seen is None:
        return _blocked_plan(
            market_id=market_id,
            condition_id=condition_id,
            entry_mode=entry_mode,
            entry_cfg=entry_cfg,
            reject_reason=REASON_EVAL_SKIP,
            selected_leg=selected_leg,
            token_id=token_id,
            expected_slippage=expected_slippage,
        )

    if fee_model is None or not fee_model.is_resolved:
        return _blocked_plan(
            market_id=market_id,
            condition_id=condition_id,
            entry_mode=entry_mode,
            entry_cfg=entry_cfg,
            reject_reason=REASON_FEE_MODEL_UNKNOWN,
            selected_leg=selected_leg,
            token_id=token_id,
            p_L=p_L,
            ask_seen=ask_seen,
            expected_slippage=expected_slippage,
        )

    if tick_size <= 0:
        return _blocked_plan(
            market_id=market_id,
            condition_id=condition_id,
            entry_mode=entry_mode,
            entry_cfg=entry_cfg,
            reject_reason=REASON_INVALID_TICK,
            selected_leg=selected_leg,
            token_id=token_id,
            p_L=p_L,
            ask_seen=ask_seen,
            expected_slippage=expected_slippage,
        )

    try:
        max_fill = max_fill_price_for_edge_floor(
            p_L,
            entry_cfg.theta_fill_floor,
            fee_model,
            expected_slippage,
            tick_size,
        )
    except EntryCapError:
        return _blocked_plan(
            market_id=market_id,
            condition_id=condition_id,
            entry_mode=entry_mode,
            entry_cfg=entry_cfg,
            reject_reason=REASON_MODEL_CAP,
            selected_leg=selected_leg,
            token_id=token_id,
            p_L=p_L,
            ask_seen=ask_seen,
            expected_slippage=expected_slippage,
        )

    final_limit = min(ask_seen, max_fill)

    from tyrex_pm.quant.fees import phi_taker_fee

    phi_limit = phi_taker_fee(final_limit, fee_model)
    pred_edge = predicted_edge_at_limit(
        p_L=p_L,
        limit_price=final_limit,
        fee_model=fee_model,
        expected_slippage=expected_slippage,
    )
    if pred_edge < entry_cfg.theta_fill_floor:
        return _blocked_plan(
            market_id=market_id,
            condition_id=condition_id,
            entry_mode=entry_mode,
            entry_cfg=entry_cfg,
            reject_reason=REASON_EDGE_FLOOR,
            selected_leg=selected_leg,
            token_id=token_id,
            p_L=p_L,
            ask_seen=ask_seen,
            expected_slippage=expected_slippage,
        )

    if sizing_cfg is None:
        return _blocked_plan(
            market_id=market_id,
            condition_id=condition_id,
            entry_mode=entry_mode,
            entry_cfg=entry_cfg,
            reject_reason=REASON_MISSING_SIZING,
            selected_leg=selected_leg,
            token_id=token_id,
            p_L=p_L,
            ask_seen=ask_seen,
            expected_slippage=expected_slippage,
        )

    try:
        sizing = compute_fixed_usd_shares(
            limit_price=final_limit,
            sizing=sizing_cfg,
            venue_min_order_size=venue_min_order_size,
        )
    except ZGapSizingError as exc:
        reason = str(exc)
        if reason not in {
            REASON_SIZE_BELOW_MIN,
            REASON_NOTIONAL_CAP,
            REASON_MISSING_LIMIT_PRICE,
            REASON_VENUE_MIN_SIZE,
        }:
            reason = REASON_SIZE_BELOW_MIN
        return _blocked_plan(
            market_id=market_id,
            condition_id=condition_id,
            entry_mode=entry_mode,
            entry_cfg=entry_cfg,
            reject_reason=reason,
            selected_leg=selected_leg,
            token_id=token_id,
            p_L=p_L,
            ask_seen=ask_seen,
            expected_slippage=expected_slippage,
        )

    return ZGapEntryPlan(
        market_id=market_id,
        condition_id=condition_id,
        selected_leg=selected_leg,
        token_id=token_id,
        p_L=p_L,
        ask_seen=ask_seen,
        theta_fill_floor=entry_cfg.theta_fill_floor,
        max_fill_price=max_fill,
        final_limit_price=final_limit,
        phi_at_limit=phi_limit,
        expected_slippage=expected_slippage,
        predicted_edge_at_limit=pred_edge,
        shares=sizing.shares,
        notional_usd=sizing.notional_usd,
        order_style=ORDER_STYLE_FAK,
        time_in_force=TIME_IN_FORCE_FAK,
        entry_mode=entry_mode,
        plan_status=PLAN_STATUS_READY,
        reject_reason=None,
        created_ts=_utc_now(),
    )


@dataclass(frozen=True)
class ZGapPreSubmitValidation:
    passed: bool
    reject_reason: str | None
    gate_results: dict[str, str]


def _gate(results: dict[str, str], name: str, ok: bool) -> bool:
    results[name] = "pass" if ok else "fail"
    return ok


def validate_z_gap_pre_submit(
    plan: ZGapEntryPlan,
    *,
    entry_mode: str,
    fee_model_status: str | None,
    sizing_cfg: ZGapSizingConfig | None = None,
    signal: SignalSnapshot | None = None,
    time_authority: TimeAuthority | None = None,
    preflight: ZGapPreflightGates | None = None,
) -> ZGapPreSubmitValidation:
    """Strict pre-submit validator — fail closed; blocks live submit unless all gates pass."""
    gates: dict[str, str] = {}

    def _fail(reason: str) -> ZGapPreSubmitValidation:
        return ZGapPreSubmitValidation(passed=False, reject_reason=reason, gate_results=dict(gates))

    if not _gate(gates, "entry_mode_enforce", entry_mode == Z_GAP_ENTRY_MODE_ENFORCE):
        return _fail(REASON_OBSERVE_MODE)
    if not _gate(gates, "plan_ready", plan.plan_status == PLAN_STATUS_READY):
        return _fail(plan.reject_reason or REASON_EVAL_SKIP)
    if not _gate(gates, "time_in_force_fak", plan.time_in_force == TIME_IN_FORCE_FAK):
        return _fail(REASON_EVAL_SKIP)
    if not _gate(gates, "selected_leg", plan.selected_leg in {LEG_UP, LEG_DOWN}):
        return _fail(REASON_MISSING_TOKEN)
    if not _gate(gates, "token_id", bool(plan.token_id)):
        return _fail(REASON_MISSING_TOKEN)

    if plan.shares is None or plan.final_limit_price is None or plan.theta_fill_floor is None:
        return _fail(REASON_EVAL_SKIP)

    sizing_cfg_min = sizing_cfg.min_shares if sizing_cfg is not None else Decimal("1")
    if not _gate(gates, "shares_min", plan.shares >= sizing_cfg_min):
        return _fail(REASON_SIZE_BELOW_MIN)

    if sizing_cfg is not None and plan.notional_usd is not None:
        if not _gate(gates, "notional_cap", plan.notional_usd <= sizing_cfg.max_usd):
            return _fail(REASON_NOTIONAL_CAP)

    if plan.notional_usd is not None and plan.ask_seen is not None:
        if not _gate(gates, "limit_le_ask", plan.final_limit_price <= plan.ask_seen):
            return _fail(REASON_LIMIT_ABOVE_ASK)

    if plan.predicted_edge_at_limit is None or not _gate(
        gates,
        "edge_floor",
        plan.predicted_edge_at_limit >= plan.theta_fill_floor,
    ):
        return _fail(REASON_EDGE_FLOOR)

    if not _gate(gates, "fee_model_resolved", fee_model_status == FEE_MODEL_STATUS_RESOLVED):
        return _fail(REASON_FEE_MODEL_UNKNOWN)

    if signal is not None:
        ptb_ok = (
            signal.price_to_beat is not None
            and signal.ptb_status in {FRESHNESS_OBSERVED, FRESHNESS_OBSERVED_FROM_LOG}
            and signal.ptb_status not in {FRESHNESS_MISSING, FRESHNESS_PENDING, FRESHNESS_LATE}
        )
        if not _gate(gates, "ptb_usable", ptb_ok):
            return _fail(REASON_PTB_UNUSABLE)
    else:
        _gate(gates, "ptb_usable", False)
        return _fail(REASON_PTB_UNUSABLE)

    sync_ok = (
        time_authority is not None
        and time_authority.sync_status == SYNC_STATUS_SYNCED
        and time_authority.uncertainty_ms is not None
        and time_authority.uncertainty_ms <= DEFAULT_ENFORCE_UNCERTAINTY_MAX_MS
    )
    if not _gate(gates, "clock_sync", sync_ok):
        return _fail(REASON_CLOCK_SYNC)

    if preflight is None:
        _gate(gates, "preflight", False)
        return _fail(REASON_PREFLIGHT)

    if not _gate(gates, "calibration_lite_reviewed", preflight.calibration_lite_reviewed):
        return _fail(REASON_CALIBRATION_NOT_REVIEWED)
    if not _gate(gates, "operator_approved_enforce", preflight.operator_approved_enforce):
        return _fail(REASON_OPERATOR_NOT_APPROVED)
    if not _gate(gates, "preflight_all", preflight.enforce_allowed):
        return _fail(REASON_PREFLIGHT)

    return ZGapPreSubmitValidation(passed=True, reject_reason=None, gate_results=dict(gates))


def z_gap_entry_plan_to_intent_work_unit(
    plan: ZGapEntryPlan,
    *,
    owner_id: str,
    validation: ZGapPreSubmitValidation,
    correlation_id: str,
) -> IntentWorkUnit | None:
    """Convert a validated plan to IntentWorkUnit — returns None unless validation passed."""
    if not validation.passed:
        return None
    if plan.plan_status != PLAN_STATUS_READY:
        return None
    if plan.token_id is None or plan.shares is None or plan.final_limit_price is None:
        return None

    intent = EnterIntent(
        token_id=TokenId(plan.token_id),
        side=Side.BUY,
        size=plan.shares,
        limit_price=plan.final_limit_price,
        order_style=OrderStyle.FAK,
    )
    return IntentWorkUnit(
        intent=intent,
        correlation_id=correlation_id,
        intent_fact_extensions={
            "source": Z_GAP_INTENT_SOURCE,
            "allocation_owner_id": owner_id,
            "selected_leg": plan.selected_leg,
            "market_id": plan.market_id,
            "condition_id": plan.condition_id,
            "predicted_edge_at_limit": str(plan.predicted_edge_at_limit),
            "theta_fill_floor": str(plan.theta_fill_floor),
        },
    )
