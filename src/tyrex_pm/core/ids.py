"""Typed identifiers for the event-driven core.

Polymarket mapping (documented, not encoded as Z-Gap specifics):

* ``MarketId`` — venue market / condition identity (shared by YES and NO).
* ``TokenId`` — tradable CLOB token identity (one outcome leg).
* ``InstrumentId`` — framework instrument key (typically wraps a token).
* External event/window slugs are *not* identifiers here; R3 schedulers may
  carry window labels separately without polluting generic IDs.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True, slots=True, order=True)
class EventId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("EventId value must be non-empty")


@dataclass(frozen=True, slots=True, order=True)
class CorrelationId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("CorrelationId value must be non-empty")


@dataclass(frozen=True, slots=True, order=True)
class InstrumentId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("InstrumentId value must be non-empty")


@dataclass(frozen=True, slots=True, order=True)
class MarketId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("MarketId value must be non-empty")


@dataclass(frozen=True, slots=True, order=True)
class TokenId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("TokenId value must be non-empty")


@dataclass(frozen=True, slots=True, order=True)
class StrategyId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("StrategyId value must be non-empty")


@dataclass(frozen=True, slots=True, order=True)
class RunId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("RunId value must be non-empty")


@dataclass(frozen=True, slots=True, order=True)
class OrderId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("OrderId value must be non-empty")


@dataclass(frozen=True, slots=True, order=True)
class ClientOrderId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("ClientOrderId value must be non-empty")


def new_event_id() -> EventId:
    return EventId(str(uuid4()))


def new_correlation_id() -> CorrelationId:
    return CorrelationId(str(uuid4()))


def new_run_id() -> RunId:
    return RunId(str(uuid4()))


def new_order_id() -> OrderId:
    return OrderId(str(uuid4()))


def new_client_order_id() -> ClientOrderId:
    return ClientOrderId(str(uuid4()))


def parse_event_id(raw: str) -> EventId:
    return EventId(raw)


def as_instrument_id(token: TokenId) -> InstrumentId:
    """Default mapping: one tradable token == one instrument."""
    return InstrumentId(token.value)
