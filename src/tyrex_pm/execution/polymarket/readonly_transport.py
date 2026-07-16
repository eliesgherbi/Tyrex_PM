"""Mutation-impossible account observation transport (R6C).

This Protocol intentionally omits submit/cancel. Callers that only depend on
``ReadOnlyAccountTransport`` cannot construct trading mutations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from tyrex_pm.execution.polymarket.transport import (
    VenueBalanceSnapshot,
    VenueOrderSnapshot,
    VenuePositionSnapshot,
    VenueTradeSnapshot,
)

UserEventHandler = Callable[[dict[str, Any]], None]


class ReadOnlyAccountTransport(Protocol):
    """Account/market observation surface with no mutation methods."""

    def get_open_orders(self, *, market_id: str | None = None) -> list[VenueOrderSnapshot]: ...

    def get_order(self, venue_order_id: str) -> VenueOrderSnapshot | None: ...

    def get_trades(
        self, *, market_id: str | None = None, after: str | None = None
    ) -> list[VenueTradeSnapshot]: ...

    def get_positions(self) -> list[VenuePositionSnapshot]: ...

    def get_balance(self) -> VenueBalanceSnapshot: ...

    def get_server_time(self) -> int | None: ...

    def subscribe_user_events(self, handler: UserEventHandler) -> None: ...

    def stop(self) -> None: ...


@dataclass(frozen=True)
class EndpointProbeResult:
    host: str
    path: str
    method: str
    category: str  # public_market_data | authenticated_account | public_data_api | infra
    status: int | str
    cloudflare_error: str | None
    app_auth_validation_reached: bool
    ok: bool
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "path": self.path,
            "method": self.method,
            "category": self.category,
            "status": self.status,
            "cloudflare_error": self.cloudflare_error,
            "app_auth_validation_reached": self.app_auth_validation_reached,
            "ok": self.ok,
            "detail": self.detail,
        }


# Endpoint taxonomy (official docs.polymarket.com authentication guide):
# Public (no auth): Gamma API, Data API, CLOB read endpoints (orderbook, prices, spreads, /time)
# Authenticated L2: open orders, balances/allowances, post/cancel/heartbeat
PUBLIC_CLOB_TIME = ("clob.polymarket.com", "/time", "GET", "public_market_data")
PUBLIC_CLOB_BOOK = ("clob.polymarket.com", "/book", "GET", "public_market_data")
AUTH_CLOB_ORDERS = ("clob.polymarket.com", "/data/orders", "GET", "authenticated_account")
AUTH_CLOB_TRADES = ("clob.polymarket.com", "/data/trades", "GET", "authenticated_account")
AUTH_CLOB_BALANCE = ("clob.polymarket.com", "/balance-allowance", "GET", "authenticated_account")
# Heartbeat is authenticated POST and can cancel opens if started then stopped — R6C forbids calling it.
AUTH_CLOB_HEARTBEAT = ("clob.polymarket.com", "/heartbeats", "POST", "authenticated_mutating")
PUBLIC_DATA_POSITIONS = ("data-api.polymarket.com", "/positions", "GET", "public_data_api")
