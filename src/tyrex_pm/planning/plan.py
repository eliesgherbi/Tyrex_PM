"""Dry execution plan types (R4 — no submission)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping
from uuid import uuid4

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import CorrelationId, EventId, InstrumentId, MarketId, TokenId
from tyrex_pm.core.intents import IntentId, OrderSide


class PlanStatus(str, Enum):
    PLANNED = "PLANNED"
    UNPLANNABLE = "UNPLANNABLE"


class PlanFailReason(str, Enum):
    MISSING_BOOK = "MISSING_BOOK"
    ONE_SIDED_BOOK = "ONE_SIDED_BOOK"
    INVALID_TICK = "INVALID_TICK"
    BELOW_MIN_SIZE = "BELOW_MIN_SIZE"
    INSUFFICIENT_DEPTH = "INSUFFICIENT_DEPTH"
    PRICE_ZERO = "PRICE_ZERO"
    MAX_PRICE_EXCEEDED = "MAX_PRICE_EXCEEDED"
    ROUNDING_FAILURE = "ROUNDING_FAILURE"
    RISK_NOT_APPROVED = "RISK_NOT_APPROVED"


@dataclass(frozen=True, slots=True)
class PlanId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("PlanId must be non-empty")


def new_plan_id() -> PlanId:
    return PlanId(str(uuid4()))


@dataclass(frozen=True, kw_only=True)
class ExecutionPlan:
    plan_id: PlanId
    intent_id: IntentId
    instrument_id: InstrumentId
    token_id: TokenId
    market_id: MarketId
    side: OrderSide
    quantity: Decimal
    limit_price: Decimal
    expected_notional: Decimal
    book_ts_event: datetime
    tick_size: Decimal | None
    min_order_size: Decimal | None
    planned_at: datetime
    correlation_id: CorrelationId
    causation_id: EventId | None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "planned_at", require_utc(self.planned_at, field_name="planned_at")
        )
        object.__setattr__(
            self,
            "book_ts_event",
            require_utc(self.book_ts_event, field_name="book_ts_event"),
        )
        object.__setattr__(self, "evidence", dict(self.evidence))


@dataclass(frozen=True, kw_only=True)
class PlanningResult:
    status: PlanStatus
    plan: ExecutionPlan | None
    fail_reason: PlanFailReason | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", dict(self.evidence))
        if self.status is PlanStatus.PLANNED and self.plan is None:
            raise ValueError("PLANNED requires plan")
        if self.status is PlanStatus.UNPLANNABLE and self.fail_reason is None:
            raise ValueError("UNPLANNABLE requires fail_reason")
