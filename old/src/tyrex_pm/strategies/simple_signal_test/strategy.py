"""Minimal non-guru strategy used to validate generic signal dispatch (P1).

It turns a :class:`SimpleSignal` into an ``EnterIntent`` (BUY) or ``ExitIntent``
(SELL) with no guru-specific plumbing. SELL sizing is clamped to the owner's
available allocation so it honours the same allocation invariant as every other
SELL path.
"""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import Side
from tyrex_pm.core.models import EnterIntent, ExitIntent
from tyrex_pm.runtime.allocation_ids import OWNER_SIMPLE_SIGNAL_TEST
from tyrex_pm.signals.simple_signal import SimpleSignal
from tyrex_pm.strategies.base import StrategyContext, StrategyResult

SIMPLE_SIGNAL_TEST_INTENT_SOURCE = "simple_signal_test_strategy"


def _resolve_size(signal: SimpleSignal) -> Decimal | None:
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


class SimpleSignalTestStrategy:
    """Stateless strategy mapping one :class:`SimpleSignal` to one intent."""

    def __init__(self, *, owner_id: str = OWNER_SIMPLE_SIGNAL_TEST) -> None:
        self._owner_id = owner_id

    @property
    def owner_id(self) -> str:
        return self._owner_id

    def on_signal(self, signal: SimpleSignal, ctx: StrategyContext) -> StrategyResult:
        size = _resolve_size(signal)
        if size is None or size <= 0:
            return StrategyResult(intents=[], skip_reason=rc.GURU_PRICE_REQUIRED)

        meta = {
            "source": SIMPLE_SIGNAL_TEST_INTENT_SOURCE,
            "allocation_owner_id": self._owner_id,
            "signal_type": signal.signal_type,
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
            exit_intent = ExitIntent(
                token_id=signal.token_id,
                side=Side.SELL,
                size=final_size,
                limit_price=signal.limit_price,
                order_style=signal.order_style,
            )
            return StrategyResult(intents=[exit_intent], skip_reason=None, meta=meta)

        enter_intent = EnterIntent(
            token_id=signal.token_id,
            side=Side.BUY,
            size=size,
            limit_price=signal.limit_price,
            order_style=signal.order_style,
        )
        return StrategyResult(intents=[enter_intent], skip_reason=None, meta=meta)
