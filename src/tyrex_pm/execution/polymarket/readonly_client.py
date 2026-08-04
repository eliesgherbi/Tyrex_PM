"""R6B authenticated read-only client — never submits or cancels.

Delegates to official ``polymarket-client`` SecureClient via SdkReadonlyTransport.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tyrex_pm.execution.polymarket.auth import L2Credentials
from tyrex_pm.execution.polymarket.sdk_readonly import SdkReadonlyTransport
from tyrex_pm.execution.polymarket.transport import (
    CancelOrderResult,
    SubmitOrderRequest,
    SubmitOrderResult,
    VenueBalanceSnapshot,
    VenueOrderSnapshot,
    VenuePositionSnapshot,
    VenueTradeSnapshot,
)


class MutationAttemptError(RuntimeError):
    """Raised if R6B client is asked to mutate."""


@dataclass
class ReadOnlyClobClient:
    """L2-authenticated reads only. ``submit_order`` / ``cancel_order`` always fail."""

    creds: L2Credentials
    base_url: str = "https://clob.polymarket.com"
    timeout_s: float = 15.0
    _transport: SdkReadonlyTransport | None = field(default=None, repr=False)

    def _ro(self) -> SdkReadonlyTransport:
        if self._transport is None:
            self._transport = SdkReadonlyTransport.from_env()
            # Prefer injected creds when from_env was not used with matching env.
            self._transport.creds = self.creds
        return self._transport

    @classmethod
    def from_creds(cls, creds: L2Credentials, *, transport: SdkReadonlyTransport | None = None):
        return cls(creds=creds, _transport=transport)

    def submit_order(self, request: SubmitOrderRequest) -> SubmitOrderResult:
        raise MutationAttemptError("R6B read-only client forbids submit_order")

    def cancel_order(self, venue_order_id: str) -> CancelOrderResult:
        raise MutationAttemptError("R6B read-only client forbids cancel_order")

    def get_open_orders(self, *, market_id: str | None = None) -> list[VenueOrderSnapshot]:
        return self._ro().get_open_orders(market_id=market_id)

    def get_order(self, venue_order_id: str) -> VenueOrderSnapshot | None:
        return self._ro().get_order(venue_order_id)

    def get_trades(
        self, *, market_id: str | None = None, after: str | None = None
    ) -> list[VenueTradeSnapshot]:
        return self._ro().get_trades(market_id=market_id, after=after)

    def get_positions(self) -> list[VenuePositionSnapshot]:
        return self._ro().get_positions()

    def get_balance(self) -> VenueBalanceSnapshot:
        return self._ro().get_balance()

    def get_server_time(self) -> int | None:
        return self._ro().get_server_time()

    def stop(self) -> None:
        if self._transport is not None:
            self._transport.stop()
