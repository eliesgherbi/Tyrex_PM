"""Transport boundary — HTTP/WS I/O separated from LiveOMS lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Mapping, Protocol


@dataclass(frozen=True, kw_only=True)
class VenueOrderSnapshot:
    venue_order_id: str
    status: str
    instrument_token_id: str
    side: str
    original_size: Decimal
    size_matched: Decimal
    price: Decimal
    market_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class VenueTradeSnapshot:
    venue_trade_id: str
    venue_order_id: str | None
    instrument_token_id: str
    side: str
    size: Decimal
    price: Decimal
    status: str
    fee_rate_bps: Decimal | None = None
    market_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class VenuePositionSnapshot:
    instrument_token_id: str
    size: Decimal
    avg_price: Decimal | None = None
    market_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class VenueBalanceSnapshot:
    collateral_balance: Decimal
    allowance: Decimal | None = None


@dataclass(frozen=True, kw_only=True)
class SubmitOrderRequest:
    """Opaque signed-order envelope — LiveOMS builds; transport posts.

    For official V2 FAK/FOK market BUY, ``amount`` is the dollar notional to
    spend and ``size`` is the max share quantity derived under the budget.
    For SELL market orders, ``amount`` is share quantity (SDK semantics).
    """

    token_id: str
    side: str
    price: str
    size: str
    order_type: str = "GTC"
    amount: str | None = None
    tick_size: str | None = None
    neg_risk: bool = False
    local_order_id: str | None = None
    salt: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class SubmitOrderResult:
    ok: bool
    venue_order_id: str | None
    status: str | None
    error: str | None = None
    uncertain: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class CancelOrderResult:
    ok: bool
    venue_order_id: str
    error: str | None = None
    uncertain: bool = False
    already_terminal: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict)


UserEventHandler = Callable[[Mapping[str, Any]], None]


class PolymarketTransport(Protocol):
    """Venue I/O. R6A tests use FakeTransport; R6B may use read-only HTTP."""

    def submit_order(self, request: SubmitOrderRequest) -> SubmitOrderResult: ...

    def cancel_order(self, venue_order_id: str) -> CancelOrderResult: ...

    def get_open_orders(self, *, market_id: str | None = None) -> list[VenueOrderSnapshot]: ...

    def get_order(self, venue_order_id: str) -> VenueOrderSnapshot | None: ...

    def get_trades(
        self, *, market_id: str | None = None, after: str | None = None
    ) -> list[VenueTradeSnapshot]: ...

    def get_positions(self) -> list[VenuePositionSnapshot]: ...

    def get_balance(self) -> VenueBalanceSnapshot: ...

    def subscribe_user_events(self, handler: UserEventHandler) -> None: ...

    def stop(self) -> None: ...
