"""Typed execution events published by OMS implementations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.core.events import Event, _validate_event_times
from tyrex_pm.core.ids import ClientOrderId, InstrumentId, OrderId
from tyrex_pm.core.intents import OrderSide
from tyrex_pm.core.numerics import as_decimal, require_non_negative


@dataclass(frozen=True, slots=True)
class ExecutionId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("ExecutionId must be non-empty")


@dataclass(frozen=True, kw_only=True)
class OrderSubmitted(Event):
    order_id: OrderId
    client_order_id: ClientOrderId
    instrument_id: InstrumentId
    side: OrderSide
    quantity: Decimal
    limit_price: Decimal

    def __post_init__(self) -> None:
        _validate_event_times(self)


@dataclass(frozen=True, kw_only=True)
class OrderAccepted(Event):
    order_id: OrderId
    venue_order_id: str | None = None

    def __post_init__(self) -> None:
        _validate_event_times(self)


@dataclass(frozen=True, kw_only=True)
class OrderRejected(Event):
    order_id: OrderId
    reason_code: str

    def __post_init__(self) -> None:
        _validate_event_times(self)
        if not self.reason_code.strip():
            raise ValueError("reason_code must be non-empty")


@dataclass(frozen=True, kw_only=True)
class OrderCancelPending(Event):
    order_id: OrderId

    def __post_init__(self) -> None:
        _validate_event_times(self)


@dataclass(frozen=True, kw_only=True)
class OrderCanceled(Event):
    order_id: OrderId
    reason_code: str

    def __post_init__(self) -> None:
        _validate_event_times(self)


@dataclass(frozen=True, kw_only=True)
class _FillEventBase(Event):
    execution_id: ExecutionId
    order_id: OrderId
    instrument_id: InstrumentId
    side: OrderSide
    fill_quantity: Decimal
    fill_price: Decimal
    fee_amount: Decimal
    fee_currency: str
    cumulative_filled: Decimal
    remaining_quantity: Decimal

    def __post_init__(self) -> None:
        _validate_event_times(self)
        object.__setattr__(
            self,
            "fill_quantity",
            require_non_negative(
                as_decimal(self.fill_quantity, field_name="fill_quantity"),
                field_name="fill_quantity",
            ),
        )
        if self.fill_quantity <= 0:
            raise ValueError("fill_quantity must be > 0")
        object.__setattr__(
            self,
            "fill_price",
            as_decimal(self.fill_price, field_name="fill_price"),
        )
        object.__setattr__(
            self,
            "fee_amount",
            require_non_negative(
                as_decimal(self.fee_amount, field_name="fee_amount"),
                field_name="fee_amount",
            ),
        )
        object.__setattr__(
            self,
            "cumulative_filled",
            require_non_negative(
                as_decimal(self.cumulative_filled, field_name="cumulative_filled"),
                field_name="cumulative_filled",
            ),
        )
        object.__setattr__(
            self,
            "remaining_quantity",
            require_non_negative(
                as_decimal(self.remaining_quantity, field_name="remaining_quantity"),
                field_name="remaining_quantity",
            ),
        )


@dataclass(frozen=True, kw_only=True)
class OrderPartiallyFilled(_FillEventBase):
    pass


@dataclass(frozen=True, kw_only=True)
class OrderFilled(_FillEventBase):
    pass
