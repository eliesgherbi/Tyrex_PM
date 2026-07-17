"""Split read vs mutation transport protocols (R7A structural gate)."""

from __future__ import annotations

from typing import Protocol

from tyrex_pm.execution.polymarket.transport import (
    CancelOrderResult,
    SubmitOrderRequest,
    SubmitOrderResult,
    UserEventHandler,
    VenueBalanceSnapshot,
    VenueOrderSnapshot,
    VenuePositionSnapshot,
    VenueTradeSnapshot,
)


class PolymarketReadTransport(Protocol):
    """Observation-only venue I/O — no submit/cancel methods."""

    def get_open_orders(self, *, market_id: str | None = None) -> list[VenueOrderSnapshot]: ...

    def get_order(self, venue_order_id: str) -> VenueOrderSnapshot | None: ...

    def get_trades(
        self, *, market_id: str | None = None, after: str | None = None
    ) -> list[VenueTradeSnapshot]: ...

    def get_positions(self) -> list[VenuePositionSnapshot]: ...

    def get_balance(self) -> VenueBalanceSnapshot: ...

    def subscribe_user_events(self, handler: UserEventHandler) -> None: ...

    def stop(self) -> None: ...


class PolymarketMutationTransport(Protocol):
    """Mutation venue I/O — must not be injected into live-preflight."""

    def submit_order(self, request: SubmitOrderRequest) -> SubmitOrderResult: ...

    def cancel_order(self, venue_order_id: str) -> CancelOrderResult: ...
