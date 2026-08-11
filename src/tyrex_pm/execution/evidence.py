"""Normalized execution evidence accepted by the authoritative reducer."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, TypeAlias
from uuid import uuid4

from tyrex_pm.execution.orders import OrderSide, OrderSpec, order_spec_from_dict, order_spec_to_dict


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("event timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


class ExecutionRole(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


class PreDispatchStage(str, Enum):
    PREPARATION = "PREPARATION"
    FINAL_GATE = "FINAL_GATE"


class TradeStatus(str, Enum):
    MATCHED = "MATCHED"
    MINED = "MINED"
    CONFIRMED = "CONFIRMED"
    RETRYING = "RETRYING"
    FAILED = "FAILED"


@dataclass(frozen=True, kw_only=True)
class EvidenceEnvelope:
    session_id: str
    event_id: str
    observed_at: datetime
    dedupe_key: str
    observed_monotonic_ns: int | None = None

    def __post_init__(self) -> None:
        for name in ("session_id", "event_id", "dedupe_key"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must be non-empty")
        object.__setattr__(self, "observed_at", _utc(self.observed_at))
        if self.observed_monotonic_ns is not None and self.observed_monotonic_ns < 0:
            raise ValueError("observed_monotonic_ns must be non-negative")


@dataclass(frozen=True, kw_only=True)
class SessionOpened(EvidenceEnvelope):
    strategy_id: str
    market_id: str
    window_id: str
    token_id: str
    baseline_position_shares: Decimal = Decimal("0")
    baseline_sellable_shares: Decimal = Decimal("0")
    baseline_open_order_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        EvidenceEnvelope.__post_init__(self)
        position = Decimal(str(self.baseline_position_shares))
        sellable = Decimal(str(self.baseline_sellable_shares))
        if position < 0 or sellable < 0:
            raise ValueError("baseline quantities must be non-negative")
        object.__setattr__(self, "baseline_position_shares", position)
        object.__setattr__(self, "baseline_sellable_shares", sellable)
        object.__setattr__(self, "baseline_open_order_ids", tuple(self.baseline_open_order_ids))


@dataclass(frozen=True, kw_only=True)
class OrderRequested(EvidenceEnvelope):
    role: ExecutionRole
    order: OrderSpec


@dataclass(frozen=True, kw_only=True)
class OrderPrepared(EvidenceEnvelope):
    role: ExecutionRole
    order_id: str
    prepared_order_digest: str
    requested_protection_price: Decimal | None = None
    effective_protection_price: Decimal | None = None
    tick_size: Decimal | None = None

    def __post_init__(self) -> None:
        EvidenceEnvelope.__post_init__(self)
        for field_name in (
            "requested_protection_price",
            "effective_protection_price",
            "tick_size",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, Decimal(str(value)))


@dataclass(frozen=True, kw_only=True)
class OrderPreDispatchFailed(EvidenceEnvelope):
    role: ExecutionRole
    order_id: str
    stage: PreDispatchStage
    error_class: str
    error_code: str
    message: str
    requested_protection_price: Decimal | None = None
    effective_protection_price: Decimal | None = None
    tick_size: Decimal | None = None

    def __post_init__(self) -> None:
        EvidenceEnvelope.__post_init__(self)
        for field_name in (
            "requested_protection_price",
            "effective_protection_price",
            "tick_size",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, Decimal(str(value)))


@dataclass(frozen=True, kw_only=True)
class DispatchAuthorized(EvidenceEnvelope):
    role: ExecutionRole
    order_id: str
    prepared_order_digest: str


@dataclass(frozen=True, kw_only=True)
class SubmissionAttempted(EvidenceEnvelope):
    role: ExecutionRole
    order_id: str
    attempt_id: str


@dataclass(frozen=True, kw_only=True)
class SubmissionResponseObserved(EvidenceEnvelope):
    role: ExecutionRole
    order_id: str
    attempt_id: str
    accepted: bool
    venue_order_id: str | None
    status: str
    cumulative_matched_shares: Decimal | None = None
    trade_ids: tuple[str, ...] = ()
    error_code: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        EvidenceEnvelope.__post_init__(self)
        if self.cumulative_matched_shares is not None:
            object.__setattr__(
                self,
                "cumulative_matched_shares",
                Decimal(str(self.cumulative_matched_shares)),
            )
        object.__setattr__(self, "trade_ids", tuple(self.trade_ids))


@dataclass(frozen=True, kw_only=True)
class SubmissionFailed(EvidenceEnvelope):
    role: ExecutionRole
    order_id: str
    attempt_id: str
    error_class: str
    message: str
    ambiguous: bool


@dataclass(frozen=True, kw_only=True)
class OrderSnapshotObserved(EvidenceEnvelope):
    source: str
    venue_order_id: str
    order_id: str | None
    side: OrderSide
    original_shares: Decimal | None
    cumulative_matched_shares: Decimal
    status: str

    def __post_init__(self) -> None:
        EvidenceEnvelope.__post_init__(self)
        if self.original_shares is not None:
            object.__setattr__(self, "original_shares", Decimal(str(self.original_shares)))
        object.__setattr__(
            self,
            "cumulative_matched_shares",
            Decimal(str(self.cumulative_matched_shares)),
        )


@dataclass(frozen=True, kw_only=True)
class TradeStatusObserved(EvidenceEnvelope):
    source: str
    venue_trade_id: str
    venue_order_id: str
    order_id: str | None
    token_id: str
    side: OrderSide
    shares: Decimal
    price: Decimal
    status: TradeStatus
    venue_event_at: datetime | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        shares = Decimal(str(self.shares))
        price = Decimal(str(self.price))
        if shares <= 0:
            raise ValueError("trade shares must be > 0")
        if price <= 0 or price > 1:
            raise ValueError("trade price must be in (0, 1]")
        object.__setattr__(self, "shares", shares)
        object.__setattr__(self, "price", price)
        if self.venue_event_at is not None:
            object.__setattr__(self, "venue_event_at", _utc(self.venue_event_at))


@dataclass(frozen=True, kw_only=True)
class BalanceObserved(EvidenceEnvelope):
    source: str
    token_id: str
    balance_shares: Decimal
    allowance_shares: Decimal | None

    def __post_init__(self) -> None:
        EvidenceEnvelope.__post_init__(self)
        object.__setattr__(self, "balance_shares", Decimal(str(self.balance_shares)))
        if self.allowance_shares is not None:
            object.__setattr__(self, "allowance_shares", Decimal(str(self.allowance_shares)))


@dataclass(frozen=True, kw_only=True)
class ExitRequested(EvidenceEnvelope):
    reason: str
    protective: bool


@dataclass(frozen=True, kw_only=True)
class ReconciliationObserved(EvidenceEnvelope):
    source: str
    complete: bool
    confirmed_position_shares: Decimal
    sellable_shares: Decimal
    open_order_ids: tuple[str, ...]
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        EvidenceEnvelope.__post_init__(self)
        object.__setattr__(
            self,
            "confirmed_position_shares",
            Decimal(str(self.confirmed_position_shares)),
        )
        object.__setattr__(self, "sellable_shares", Decimal(str(self.sellable_shares)))
        object.__setattr__(self, "open_order_ids", tuple(self.open_order_ids))
        object.__setattr__(self, "notes", tuple(self.notes))


@dataclass(frozen=True, kw_only=True)
class ManualInterventionRequired(EvidenceEnvelope):
    reason: str


ExecutionEvidence: TypeAlias = (
    SessionOpened
    | OrderRequested
    | OrderPrepared
    | OrderPreDispatchFailed
    | DispatchAuthorized
    | SubmissionAttempted
    | SubmissionResponseObserved
    | SubmissionFailed
    | OrderSnapshotObserved
    | TradeStatusObserved
    | BalanceObserved
    | ExitRequested
    | ReconciliationObserved
    | ManualInterventionRequired
)


_EVENT_TYPES = {
    cls.__name__: cls
    for cls in (
        SessionOpened,
        OrderRequested,
        OrderPrepared,
        OrderPreDispatchFailed,
        DispatchAuthorized,
        SubmissionAttempted,
        SubmissionResponseObserved,
        SubmissionFailed,
        OrderSnapshotObserved,
        TradeStatusObserved,
        BalanceObserved,
        ExitRequested,
        ReconciliationObserved,
        ManualInterventionRequired,
    )
}


def new_envelope(*, session_id: str, dedupe_key: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "event_id": str(uuid4()),
        "observed_at": utc_now(),
        "dedupe_key": dedupe_key,
        "observed_monotonic_ns": time.monotonic_ns(),
    }


def evidence_to_dict(event: ExecutionEvidence) -> dict[str, Any]:
    payload = asdict(event)
    payload["event_type"] = type(event).__name__
    payload["observed_at"] = event.observed_at.isoformat()
    if isinstance(event, OrderRequested):
        payload["role"] = event.role.value
        payload["order"] = order_spec_to_dict(event.order)
    elif isinstance(
        event,
        (
            OrderPrepared,
            OrderPreDispatchFailed,
            DispatchAuthorized,
            SubmissionAttempted,
            SubmissionResponseObserved,
            SubmissionFailed,
        ),
    ):
        payload["role"] = event.role.value
    if isinstance(event, OrderPreDispatchFailed):
        payload["stage"] = event.stage.value
    if isinstance(event, (OrderSnapshotObserved, TradeStatusObserved)):
        payload["side"] = event.side.value
    if isinstance(event, TradeStatusObserved):
        payload["status"] = event.status.value
        payload["venue_event_at"] = (
            None if event.venue_event_at is None else event.venue_event_at.isoformat()
        )
    for key, value in tuple(payload.items()):
        if isinstance(value, Decimal):
            payload[key] = str(value)
    return payload


def evidence_from_dict(payload: Mapping[str, Any]) -> ExecutionEvidence:
    data = dict(payload)
    event_type = str(data.pop("event_type"))
    try:
        cls = _EVENT_TYPES[event_type]
    except KeyError as exc:
        raise ValueError(f"unsupported execution event type: {event_type}") from exc
    data["observed_at"] = datetime.fromisoformat(str(data["observed_at"]))
    if cls is OrderRequested:
        data["role"] = ExecutionRole(str(data["role"]))
        data["order"] = order_spec_from_dict(data["order"])
    elif cls in {
        OrderPrepared,
        OrderPreDispatchFailed,
        DispatchAuthorized,
        SubmissionAttempted,
        SubmissionResponseObserved,
        SubmissionFailed,
    }:
        data["role"] = ExecutionRole(str(data["role"]))
    if cls is OrderPreDispatchFailed:
        data["stage"] = PreDispatchStage(str(data["stage"]))
    if cls in {OrderSnapshotObserved, TradeStatusObserved}:
        data["side"] = OrderSide(str(data["side"]))
    if cls is TradeStatusObserved:
        data["status"] = TradeStatus(str(data["status"]))
        if data.get("venue_event_at") is not None:
            data["venue_event_at"] = datetime.fromisoformat(str(data["venue_event_at"]))
    return cls(**data)
