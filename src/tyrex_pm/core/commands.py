"""Immutable execution commands (R5 — not venue payloads)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping
from uuid import uuid4

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import (
    ClientOrderId,
    CorrelationId,
    EventId,
    InstrumentId,
    MarketId,
    OrderId,
    StrategyId,
    new_client_order_id,
    new_order_id,
)
from tyrex_pm.core.intents import IntentId, OrderSide
from tyrex_pm.core.numerics import as_decimal, require_non_negative
from tyrex_pm.planning.plan import PlanId

__all__ = [
    "CancelOrderCommand",
    "CommandId",
    "ExecutionPolicy",
    "SubmitOrderCommand",
    "new_command_id",
    "new_client_order_id",
    "new_order_id",
]


class ExecutionPolicy(str, Enum):
    NORMAL = "NORMAL"
    URGENT = "URGENT"


@dataclass(frozen=True, slots=True)
class CommandId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("CommandId must be non-empty")


def new_command_id() -> CommandId:
    return CommandId(str(uuid4()))


@dataclass(frozen=True, kw_only=True)
class SubmitOrderCommand:
    command_id: CommandId
    plan_id: PlanId
    intent_id: IntentId
    strategy_id: StrategyId
    instrument_id: InstrumentId
    market_id: MarketId
    side: OrderSide
    quantity: Decimal
    limit_price: Decimal
    client_order_id: ClientOrderId
    created_at: datetime
    correlation_id: CorrelationId
    causation_id: EventId | None
    execution_policy: ExecutionPolicy = ExecutionPolicy.NORMAL
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", require_utc(self.created_at, field_name="created_at"))
        qty = require_non_negative(
            as_decimal(self.quantity, field_name="quantity"), field_name="quantity"
        )
        if qty <= 0:
            raise ValueError("quantity must be > 0")
        object.__setattr__(self, "quantity", qty)
        price = as_decimal(self.limit_price, field_name="limit_price")
        if price < 0:
            raise ValueError("limit_price must be >= 0")
        object.__setattr__(self, "limit_price", price)
        object.__setattr__(self, "evidence", dict(self.evidence))


@dataclass(frozen=True, kw_only=True)
class CancelOrderCommand:
    command_id: CommandId
    order_id: OrderId
    reason_code: str
    created_at: datetime
    correlation_id: CorrelationId
    causation_id: EventId | None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", require_utc(self.created_at, field_name="created_at"))
        if not self.reason_code.strip():
            raise ValueError("reason_code must be non-empty")
        object.__setattr__(self, "evidence", dict(self.evidence))
