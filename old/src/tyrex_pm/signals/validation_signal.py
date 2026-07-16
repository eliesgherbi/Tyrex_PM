"""Validation harness signal (P4.5 architecture_enhance).

Operator-controlled signal for architecture validation modes (entry, urgent exit,
stale-book deny, protection triggers). Not a production signal source.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import TokenId

SIGNAL_SOURCE_VALIDATION_HARNESS = "validation_harness"
VALIDATION_SIGNAL_TYPE = "validation_action"


@dataclass(frozen=True)
class ValidationSignal:
    """A configured validation action from the harness YAML."""

    token_id: TokenId
    side: Side
    order_style: OrderStyle
    dedup_key: str
    mode: str
    size: Decimal | None = None
    notional_usd: Decimal | None = None
    limit_price: Decimal | None = None
    urgency: str = "normal"
    signal_type: str = VALIDATION_SIGNAL_TYPE
    source: str = SIGNAL_SOURCE_VALIDATION_HARNESS
