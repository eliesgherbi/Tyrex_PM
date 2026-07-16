"""Architecture validation harness strategy (P4.5).

Maps a :class:`ValidationSignal` to ``EnterIntent`` or ``ExitIntent`` without
venue I/O or store mutation. SELL sizing clamps to owner allocation.
"""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import Side
from tyrex_pm.core.models import EnterIntent, ExitIntent, URGENCY_NORMAL, URGENCY_URGENT
from tyrex_pm.runtime.allocation_ids import OWNER_VALIDATION_HARNESS, VALIDATION_HARNESS_INTENT_SOURCE
from tyrex_pm.signals.validation_signal import ValidationSignal
from tyrex_pm.strategies.base import StrategyContext, StrategyResult


def _resolve_size(signal: ValidationSignal) -> Decimal | None:
    if signal.size is not None and signal.size > 0:
        return signal.size
    if (
        signal.notional_usd is not None
        and signal.notional_usd > 0
        and signal.limit_price is not None
        and signal.limit_price > 0
    ):
        return signal.notional_usd / signal.limit_price
    return None


class ValidationHarnessStrategy:
    """Stateless strategy for operator validation modes."""

    def __init__(self, *, owner_id: str = OWNER_VALIDATION_HARNESS) -> None:
        self._owner_id = owner_id

    @property
    def owner_id(self) -> str:
        return self._owner_id

    def on_signal(self, signal: ValidationSignal, ctx: StrategyContext) -> StrategyResult:
        size = _resolve_size(signal)
        if size is None or size <= 0:
            return StrategyResult(intents=[], skip_reason=rc.GURU_PRICE_REQUIRED)

        urgency = signal.urgency or URGENCY_NORMAL
        meta = {
            "source": VALIDATION_HARNESS_INTENT_SOURCE,
            "allocation_owner_id": self._owner_id,
            "signal_type": signal.signal_type,
            "validation_mode": signal.mode,
        }

        if signal.side == Side.SELL:
            available = Decimal("0")
            ledger = ctx.coord.allocation_ledger
            if ledger is not None:
                available = ledger.get_available_allocated(self._owner_id, signal.token_id)
            final_size = min(size, available)
            meta["allocated_available"] = str(available)
            meta["planned_before_clamp"] = str(size)
            meta["final_size"] = str(final_size)
            if final_size <= 0:
                return StrategyResult(intents=[], skip_reason=rc.NAKED_SELL, meta=meta)
            exit_urgency = (
                URGENCY_URGENT
                if signal.mode in ("urgent_exit", "stale_book_deny") or urgency == URGENCY_URGENT
                else URGENCY_NORMAL
            )
            exit_intent = ExitIntent(
                token_id=signal.token_id,
                side=Side.SELL,
                size=final_size,
                limit_price=signal.limit_price,
                order_style=signal.order_style,
                urgency=exit_urgency,
            )
            return StrategyResult(intents=[exit_intent], skip_reason=None, meta=meta)

        enter_intent = EnterIntent(
            token_id=signal.token_id,
            side=Side.BUY,
            size=size,
            limit_price=signal.limit_price,
            order_style=signal.order_style,
            urgency=urgency,
        )
        return StrategyResult(intents=[enter_intent], skip_reason=None, meta=meta)
