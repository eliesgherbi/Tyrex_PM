"""Small decision helpers shared by strategies (entry price cap, outcome instrument)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tyrex_pm.core.instruments import Instrument, OutcomeSide

# Highest limit price a binary-outcome entry may pay (a 1.00 share can only lose fees).
MAX_ENTRY_PRICE = Decimal("0.99")


def capped_entry_price(ask: Decimal, pad: Decimal, cap: Decimal = MAX_ENTRY_PRICE) -> Decimal:
    """Limit price for a taker entry: the ask plus a slippage pad, never above ``cap``."""
    return min(ask + pad, cap)


def outcome_instrument(market: Any, outcome: OutcomeSide) -> Instrument:
    """The market's instrument for an outcome (YES is UP, NO is DOWN)."""
    return market.yes if outcome is OutcomeSide.YES else market.no
