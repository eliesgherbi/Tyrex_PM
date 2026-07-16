"""Immutable trading intents (R4: EnterIntent only).

ExitIntent / CancelIntent / FlattenIntent are deferred to R5 when orders,
positions, and execution feedback exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping
from uuid import uuid4

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import CorrelationId, EventId, InstrumentId, MarketId, StrategyId
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.numerics import as_decimal, require_non_negative


class IntentKind(str, Enum):
    ENTER = "ENTER"


class OrderSide(str, Enum):
    BUY = "BUY"


@dataclass(frozen=True, slots=True)
class IntentId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("IntentId value must be non-empty")


def new_intent_id() -> IntentId:
    return IntentId(str(uuid4()))


@dataclass(frozen=True, kw_only=True)
class EnterIntent:
    """Strategy-level request to acquire an outcome token up to a notional."""

    intent_id: IntentId
    strategy_id: StrategyId
    instrument_id: InstrumentId
    market_id: MarketId
    created_at: datetime
    correlation_id: CorrelationId
    causation_id: EventId | None
    reason_code: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    side: OrderSide = OrderSide.BUY
    target_notional: Decimal = Decimal("0")
    outcome: OutcomeSide = OutcomeSide.UNKNOWN
    decision_epoch: int = 0
    max_price: Decimal | None = None
    kind: IntentKind = IntentKind.ENTER

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", require_utc(self.created_at, field_name="created_at"))
        notional = require_non_negative(
            as_decimal(self.target_notional, field_name="target_notional"),
            field_name="target_notional",
        )
        if notional <= 0:
            raise ValueError("target_notional must be > 0")
        object.__setattr__(self, "target_notional", notional)
        if self.max_price is not None:
            object.__setattr__(
                self,
                "max_price",
                as_decimal(self.max_price, field_name="max_price"),
            )
        if self.side is not OrderSide.BUY:
            raise ValueError("R4 EnterIntent supports BUY only")
        if self.kind is not IntentKind.ENTER:
            raise ValueError("EnterIntent.kind must be ENTER")
        if self.decision_epoch < 0:
            raise ValueError("decision_epoch must be >= 0")
        object.__setattr__(self, "evidence", dict(self.evidence))
        if not self.reason_code.strip():
            raise ValueError("reason_code must be non-empty")

    def semantic_key(self) -> str:
        """Stable deduplication key (not the random intent_id)."""
        return "|".join(
            (
                self.strategy_id.value,
                self.market_id.value,
                self.instrument_id.value,
                self.kind.value,
                str(self.decision_epoch),
                self.outcome.value,
            )
        )
