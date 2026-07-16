"""Z-Gap FAK SELL exit planning (A0.7)."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import ExitIntent, URGENCY_URGENT
from tyrex_pm.market_data.book_read import PairBookSnapshot
from tyrex_pm.protection.sizing import ExitSizing, compute_exit_sizing
from tyrex_pm.protection.config import ProtectionPolicy, SIZE_MODE_FULL
from tyrex_pm.quant.edge import LEG_DOWN, LEG_UP
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.intent_work import IntentWorkUnit
from tyrex_pm.strategies.z_gap.entry_plan import Z_GAP_INTENT_SOURCE
from tyrex_pm.strategies.z_gap.state import ZGapLifecycleState

Z_GAP_EXIT_INTENT_SOURCE = "z_gap_exit"


def _bid_for_leg(books: PairBookSnapshot, leg: str) -> Decimal | None:
    if leg == LEG_UP:
        return books.up.bid
    if leg == LEG_DOWN:
        return books.down.bid
    return None


def clamp_exit_to_allocation(
    coord: RuntimeCoordinator,
    *,
    owner_id: str,
    token_id: TokenId,
    planned: Decimal,
) -> ExitSizing:
    policy = ProtectionPolicy(size_mode=SIZE_MODE_FULL, exit_order_style=OrderStyle.FAK)
    sizing = compute_exit_sizing(coord, owner_id=owner_id, token_id=token_id, policy=policy)
    final = min(planned, sizing.owner_allocation)
    if sizing.venue_available > 0:
        final = min(final, sizing.venue_available)
    if final < 0:
        final = Decimal("0")
    return ExitSizing(
        planned_before_clamp=planned,
        owner_allocation=sizing.owner_allocation,
        venue_available=sizing.venue_available,
        final_size=final,
    )


def build_z_gap_exit_work_unit(
    lifecycle: ZGapLifecycleState,
    *,
    books: PairBookSnapshot,
    coord: RuntimeCoordinator,
    exit_reason: str,
    correlation_id: str,
) -> IntentWorkUnit | None:
    if lifecycle.token_id is None or lifecycle.selected_leg is None:
        return None
    qty = lifecycle.active_quantity
    if qty <= 0:
        return None

    token_id = TokenId(lifecycle.token_id)
    sizing = clamp_exit_to_allocation(
        coord,
        owner_id=lifecycle.owner_id,
        token_id=token_id,
        planned=qty,
    )
    if sizing.final_size <= 0:
        return None

    bid = _bid_for_leg(books, lifecycle.selected_leg)
    if bid is None or bid <= 0:
        return None

    exit_intent = ExitIntent(
        token_id=token_id,
        side=Side.SELL,
        size=sizing.final_size,
        limit_price=bid,
        order_style=OrderStyle.FAK,
        urgency=URGENCY_URGENT,
    )
    ext: dict[str, object] = {
        "source": Z_GAP_EXIT_INTENT_SOURCE,
        "allocation_owner_id": lifecycle.owner_id,
        "z_gap_exit_reason": exit_reason,
        "z_gap_sizing": sizing.to_evidence(),
        "reduce_only_context": exit_reason,
        "selected_leg": lifecycle.selected_leg,
        "market_id": lifecycle.market_id,
    }
    return IntentWorkUnit(
        intent=exit_intent,
        correlation_id=correlation_id,
        intent_fact_extensions=ext,
    )
