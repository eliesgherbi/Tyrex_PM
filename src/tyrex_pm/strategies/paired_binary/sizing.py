"""Paired binary sizing helpers (Phase 4.6)."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import ExitIntent, URGENCY_URGENT
from tyrex_pm.protection.config import ProtectionPolicy, SIZE_MODE_FULL
from tyrex_pm.protection.sizing import ExitSizing, compute_exit_sizing
from tyrex_pm.runtime.allocation_ids import PAIRED_BINARY_INTENT_SOURCE
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.intent_work import IntentWorkUnit


def clamp_exit_size(
    coord: RuntimeCoordinator,
    *,
    owner_id: str,
    token_id: TokenId,
    planned: Decimal,
    exit_order_style: OrderStyle,
) -> ExitSizing:
    policy = ProtectionPolicy(size_mode=SIZE_MODE_FULL, exit_order_style=exit_order_style)
    sizing = compute_exit_sizing(coord, owner_id=owner_id, token_id=token_id, policy=policy)
    final = min(planned, sizing.final_size)
    if final < 0:
        final = Decimal("0")
    return ExitSizing(
        planned_before_clamp=planned,
        owner_allocation=sizing.owner_allocation,
        venue_available=sizing.venue_available,
        final_size=final,
    )


def build_exit_work_unit(
    *,
    token_id: TokenId,
    size: Decimal,
    limit_price: Decimal,
    order_style: OrderStyle,
    owner_id: str,
    pair_correlation_id: str,
    leg: str,
    leg_correlation_id: str,
    reason: str,
    sizing: ExitSizing,
) -> IntentWorkUnit | None:
    if size <= 0:
        return None
    exit_intent = ExitIntent(
        token_id=token_id,
        side=Side.SELL,
        size=size,
        limit_price=limit_price,
        order_style=order_style,
        urgency=URGENCY_URGENT,
    )
    ext: dict[str, object] = {
        "source": PAIRED_BINARY_INTENT_SOURCE,
        "allocation_owner_id": owner_id,
        "pair_correlation_id": pair_correlation_id,
        "leg": leg,
        "leg_correlation_id": leg_correlation_id,
        "paired_binary_reason": reason,
        "paired_binary_sizing": sizing.to_evidence(),
    }
    return IntentWorkUnit(
        intent=exit_intent,
        correlation_id=leg_correlation_id,
        intent_fact_extensions=ext,
    )
