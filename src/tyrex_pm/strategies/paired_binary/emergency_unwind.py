"""Generic emergency reduce-only unwind with retry (Phase 4.6 robustness)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Awaitable, Callable

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.runtime.config import PairedBinaryStrategyConfig
from tyrex_pm.strategies.paired_binary.entry_eval import LegBook
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState


@dataclass(frozen=True)
class UnwindLegResult:
    leg: str
    submitted: bool
    blocked: bool
    risk_reason: str | None
    allocation_qty: Decimal
    venue_available_qty: Decimal
    book_bid: Decimal | None
    book_age_ms: int | None
    final_size: Decimal


@dataclass(frozen=True)
class EmergencyUnwindOutcome:
    flat: bool
    manual_intervention: bool
    attempt_count: int


UnwindLegFn = Callable[..., Awaitable[UnwindLegResult]]


async def run_emergency_unwind_with_retry(
    *,
    cfg: PairedBinaryStrategyConfig,
    state: PairedBinaryRuntimeState,
    yes_book: LegBook,
    no_book: LegBook,
    qty: Decimal,
    reason: str,
    unwind_leg_fn: UnwindLegFn,
    emit_started,
    emit_attempt,
    emit_blocked,
    emit_retry,
    emit_done,
    emit_manual,
    get_leg_qty: Callable[[str], Decimal],
) -> EmergencyUnwindOutcome:
    """Retry emergency unwind until flat, timeout, or manual intervention required."""
    emit_started(reason=reason)
    deadline = monotonic_s() + cfg.activation_unwind_retry_s
    attempt = 0
    state.phase = PairedBinaryPhase.UNWIND_PENDING
    state.unwind_block_reason = reason

    while monotonic_s() < deadline:
        attempt += 1
        yes_qty = get_leg_qty("yes")
        no_qty = get_leg_qty("no")
        if yes_qty <= 0 and no_qty <= 0:
            emit_done(reason=reason, attempt_count=attempt)
            return EmergencyUnwindOutcome(flat=True, manual_intervention=False, attempt_count=attempt)

        for leg, token_id, book in (
            ("yes", state.yes_token_id, yes_book),
            ("no", state.no_token_id, no_book),
        ):
            leg_qty = get_leg_qty(leg)
            if leg_qty <= 0:
                continue
            result = await unwind_leg_fn(
                leg=leg,
                token_id=TokenId(token_id),
                qty=min(qty, leg_qty),
                reason=reason,
                book=book,
            )
            emit_attempt(result=result, attempt_count=attempt, reason=reason)
            if result.blocked:
                emit_blocked(result=result, attempt_count=attempt, reason=reason)

        yes_qty = get_leg_qty("yes")
        no_qty = get_leg_qty("no")
        if yes_qty <= 0 and no_qty <= 0:
            emit_done(reason=reason, attempt_count=attempt)
            return EmergencyUnwindOutcome(flat=True, manual_intervention=False, attempt_count=attempt)

        if monotonic_s() + cfg.activation_unwind_retry_interval_s > deadline:
            break
        emit_retry(attempt_count=attempt, reason=reason)
        await asyncio.sleep(cfg.activation_unwind_retry_interval_s)

    emit_manual(reason=reason, attempt_count=attempt)
    return EmergencyUnwindOutcome(flat=False, manual_intervention=True, attempt_count=attempt)
