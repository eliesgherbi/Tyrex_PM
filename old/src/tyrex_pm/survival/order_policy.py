"""Survival enforce exit order-type selection (Phase 1)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.models import URGENCY_NORMAL, URGENCY_URGENT
from tyrex_pm.runtime.config import SurvivalExitOrderPolicyConfig

DEFAULT_TICK = Decimal("0.01")

FAK_NO_MATCH_MARKERS = (
    "no orders found to match",
    "fak order",
    "partially filled or killed",
)


class SurvivalExitOrderType(str, Enum):
    FAK = "FAK"
    FOK = "FOK"
    GTC = "GTC"
    GTD = "GTD"


class SurvivalExitOrderPolicyMode(str, Enum):
    IMMEDIATE_ONLY = "immediate_only"
    FAK_RETRY = "fak_retry"
    FAK_THEN_MANAGED_REST = "fak_then_managed_rest"


class SurvivalRestingState(str, Enum):
    RESTING_PLACED = "RESTING_PLACED"
    RESTING_PARTIAL_FILL = "RESTING_PARTIAL_FILL"
    RESTING_FILLED = "RESTING_FILLED"
    RESTING_CANCEL_REQUESTED = "RESTING_CANCEL_REQUESTED"
    RESTING_CANCELLED = "RESTING_CANCELLED"
    RESTING_REPLACE_SCHEDULED = "RESTING_REPLACE_SCHEDULED"
    RESTING_ABANDONED = "RESTING_ABANDONED"


@dataclass(frozen=True)
class SurvivalExitOrderDecision:
    order_type: SurvivalExitOrderType
    price: Decimal
    qty: Decimal
    reason: str
    local_ttl_s: float | None
    requires_cancel_watchdog: bool
    allow_partial: bool
    urgency: str
    policy_mode: str
    attempt_count: int
    reprice_ticks_applied: int = 0

    @property
    def order_style(self) -> OrderStyle:
        if self.order_type == SurvivalExitOrderType.FOK:
            return OrderStyle.FOK
        if self.order_type in {SurvivalExitOrderType.GTC, SurvivalExitOrderType.GTD}:
            return OrderStyle.GTC
        return OrderStyle.FAK


def is_retryable_fak_reject(error_msg: str | None) -> bool:
    if not error_msg:
        return False
    lower = str(error_msg).lower()
    return any(m in lower for m in FAK_NO_MATCH_MARKERS)


def _tick_steps(cfg: SurvivalExitOrderPolicyConfig, side: Side, attempt_index: int) -> int:
    ticks = cfg.sell_reprice_ticks if side == Side.SELL else cfg.buy_reprice_ticks
    if not ticks:
        return 0
    idx = min(max(attempt_index, 0), len(ticks) - 1)
    return int(ticks[idx])


def reprice_for_attempt(
    base_price: Decimal,
    *,
    side: Side,
    attempt_index: int,
    cfg: SurvivalExitOrderPolicyConfig,
    tick_size: Decimal = DEFAULT_TICK,
) -> tuple[Decimal, int]:
    steps = _tick_steps(cfg, side, attempt_index)
    if steps <= 0 or not cfg.reprice_on_retry:
        return base_price, 0
    delta = tick_size * Decimal(steps)
    if side == Side.SELL:
        return max(Decimal("0.01"), base_price - delta), steps
    return min(Decimal("0.99"), base_price + delta), steps


def select_survival_exit_order(
    *,
    cfg: SurvivalExitOrderPolicyConfig,
    side: Side,
    qty: Decimal,
    touch_price: Decimal,
    executable_price: Decimal | None,
    attempt_count: int,
    fak_reject_count: int,
    seconds_to_close: float | None,
    allow_partial: bool,
    resting_order_open: bool,
    trigger_type: str,
    module: str,
) -> SurvivalExitOrderDecision | None:
    """Return order decision or None if policy says abandon/wait."""
    if cfg.post_only_for_survival_exit:
        return None

    mode = SurvivalExitOrderPolicyMode(cfg.mode)
    base = executable_price or touch_price
    near_close_urgent = (
        seconds_to_close is not None and seconds_to_close <= cfg.urgent_when_seconds_to_close_lt
    )
    disable_managed = (
        seconds_to_close is not None
        and seconds_to_close <= cfg.disable_managed_rest_when_seconds_to_close_lt
    )

    if resting_order_open:
        return None

    # Near close: FAK only, no resting.
    if near_close_urgent or disable_managed:
        price, ticks = reprice_for_attempt(
            base, side=side, attempt_index=fak_reject_count, cfg=cfg
        )
        return SurvivalExitOrderDecision(
            order_type=SurvivalExitOrderType.FAK,
            price=price,
            qty=qty,
            reason="near_close_fak_only",
            local_ttl_s=None,
            requires_cancel_watchdog=False,
            allow_partial=allow_partial,
            urgency=URGENCY_URGENT,
            policy_mode=mode.value,
            attempt_count=attempt_count,
            reprice_ticks_applied=ticks,
        )

    if mode == SurvivalExitOrderPolicyMode.IMMEDIATE_ONLY:
        if attempt_count > 0:
            return None
        return SurvivalExitOrderDecision(
            order_type=SurvivalExitOrderType.FAK,
            price=base,
            qty=qty,
            reason="immediate_only_first_fak",
            local_ttl_s=None,
            requires_cancel_watchdog=False,
            allow_partial=allow_partial,
            urgency=URGENCY_URGENT,
            policy_mode=mode.value,
            attempt_count=attempt_count,
        )

    # First attempt: FAK
    if fak_reject_count == 0 and attempt_count == 0:
        return SurvivalExitOrderDecision(
            order_type=SurvivalExitOrderType.FAK,
            price=base,
            qty=qty,
            reason="first_survival_enforce_fak",
            local_ttl_s=None,
            requires_cancel_watchdog=False,
            allow_partial=allow_partial,
            urgency=URGENCY_URGENT,
            policy_mode=mode.value,
            attempt_count=attempt_count,
        )

    # FAK retries
    if fak_reject_count > 0 and fak_reject_count <= cfg.max_fak_retries:
        price, ticks = reprice_for_attempt(
            base, side=side, attempt_index=fak_reject_count, cfg=cfg
        )
        ot = SurvivalExitOrderType(cfg.retry_order_type.upper())
        if ot == SurvivalExitOrderType.FOK and allow_partial:
            ot = SurvivalExitOrderType.FAK
        return SurvivalExitOrderDecision(
            order_type=ot,
            price=price,
            qty=qty,
            reason="fak_reject_retry",
            local_ttl_s=None,
            requires_cancel_watchdog=False,
            allow_partial=allow_partial,
            urgency=URGENCY_URGENT,
            policy_mode=mode.value,
            attempt_count=attempt_count,
            reprice_ticks_applied=ticks,
        )

    # Managed rest after FAK exhausted
    if (
        mode == SurvivalExitOrderPolicyMode.FAK_THEN_MANAGED_REST
        and cfg.managed_rest_enabled
        and not disable_managed
        and fak_reject_count > cfg.max_fak_retries
    ):
        managed_attempt = max(0, attempt_count - cfg.max_fak_retries - 1)
        if managed_attempt >= cfg.managed_rest_max_attempts:
            return None
        rest_type = SurvivalExitOrderType(cfg.managed_rest_order_type.upper())
        if rest_type == SurvivalExitOrderType.GTD:
            rest_type = SurvivalExitOrderType.GTC
        price, ticks = reprice_for_attempt(
            touch_price, side=side, attempt_index=managed_attempt, cfg=cfg
        )
        return SurvivalExitOrderDecision(
            order_type=rest_type,
            price=price,
            qty=qty,
            reason="managed_rest_after_fak_exhausted",
            local_ttl_s=cfg.managed_rest_local_ttl_s,
            requires_cancel_watchdog=True,
            allow_partial=True,
            urgency=URGENCY_NORMAL,
            policy_mode=mode.value,
            attempt_count=attempt_count,
            reprice_ticks_applied=ticks,
        )

    return None


def order_decision_payload(
    decision: SurvivalExitOrderDecision,
    *,
    module: str,
    trigger_type: str,
    survivor_leg: str,
    side: Side,
    time_to_close: float | None,
    depth_available: Decimal | None = None,
    order_id: str | None = None,
    previous_order_id: str | None = None,
    cancel_order_id: str | None = None,
    extra: dict | None = None,
) -> dict:
    payload = {
        "module": module,
        "trigger_type": trigger_type,
        "attempt_count": decision.attempt_count,
        "order_type": decision.order_type.value,
        "policy_mode": decision.policy_mode,
        "reason_for_order_type": decision.reason,
        "survivor_leg": survivor_leg,
        "side": side.value,
        "qty": str(decision.qty),
        "price": str(decision.price),
        "time_to_close": time_to_close,
        "allow_partial": decision.allow_partial,
        "local_ttl_s": decision.local_ttl_s,
        "reprice_ticks_applied": decision.reprice_ticks_applied,
    }
    if depth_available is not None:
        payload["depth_available"] = str(depth_available)
    if order_id:
        payload["order_id"] = order_id
    if previous_order_id:
        payload["previous_order_id"] = previous_order_id
    if cancel_order_id:
        payload["cancel_order_id"] = cancel_order_id
    if extra:
        payload.update(extra)
    return payload
