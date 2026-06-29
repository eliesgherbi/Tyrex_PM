"""Minimal non-guru signal used by the ``simple_signal_test`` harness (P1).

Proves a venue-independent signal can flow through the same generic
``process_signals`` dispatch as guru copy and produce an ``EnterIntent``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import TokenId

SIGNAL_SOURCE_SIMPLE_TEST = "simple_signal_test"
SIMPLE_SIGNAL_TYPE_ENTRY = "simple_entry"


@dataclass(frozen=True)
class SimpleSignal:
    """A self-contained entry/exit instruction from a non-guru source.

    ``size`` may be omitted when ``notional_usd`` and ``limit_price`` are set;
    the strategy derives ``size = notional_usd / limit_price`` in that case.
    """

    token_id: TokenId
    side: Side
    order_style: OrderStyle
    dedup_key: str
    size: Decimal | None = None
    notional_usd: Decimal | None = None
    limit_price: Decimal | None = None
    signal_type: str = SIMPLE_SIGNAL_TYPE_ENTRY
    source: str = SIGNAL_SOURCE_SIMPLE_TEST
