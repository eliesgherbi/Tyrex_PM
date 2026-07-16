"""Protection policy configuration (P4 architecture_enhance)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.core.enums import OrderStyle

#: Exit sizing modes for a fired protection trigger.
SIZE_MODE_FULL = "full"
SIZE_MODE_PERCENT = "percent"
SIZE_MODE_FIXED = "fixed"
ProtectionSizeMode = str


@dataclass(frozen=True)
class ProtectionPolicy:
    """How a single owner_id/token position is protected.

    Triggers may be expressed as percentages off the entry price or as absolute
    prices. Absolute prices take precedence when both are set (mirrors the
    legacy tp_sl_test harness so behavior is recognizable).
    """

    take_profit_pct: Decimal | None = None
    stop_loss_pct: Decimal | None = None
    take_profit_price: Decimal | None = None
    stop_loss_price: Decimal | None = None
    size_mode: ProtectionSizeMode = SIZE_MODE_FULL
    fixed_size: Decimal | None = None
    percent: Decimal | None = None
    #: Exit order style; protection exits are marketable so FAK is the default.
    exit_order_style: OrderStyle = OrderStyle.FAK
    #: Optional fallback limit used by the planner only if a fresh book is missing
    #: and an explicit urgent-exit fallback is configured.
    exit_limit_price: Decimal | None = None
    #: Max book age (s) beyond which a mark is treated as stale (no trigger).
    max_book_age_s: float = 5.0

    def has_triggers(self) -> bool:
        return any(
            v is not None
            for v in (
                self.take_profit_pct,
                self.stop_loss_pct,
                self.take_profit_price,
                self.stop_loss_price,
            )
        )
