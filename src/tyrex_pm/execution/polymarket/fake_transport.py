"""Scripted fake transport for R6A — realistic venue payloads, no network."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any, Callable

from tyrex_pm.execution.polymarket.transport import (
    CancelOrderResult,
    PolymarketTransport,
    SubmitOrderRequest,
    SubmitOrderResult,
    VenueBalanceSnapshot,
    VenueOrderSnapshot,
    VenuePositionSnapshot,
    VenueTradeSnapshot,
)


class FakeTransport:
    """Implements PolymarketTransport with scripted responses."""

    def __init__(self) -> None:
        self.open_orders: dict[str, VenueOrderSnapshot] = {}
        self.trades: list[VenueTradeSnapshot] = []
        self.positions: list[VenuePositionSnapshot] = []
        self.balance = VenueBalanceSnapshot(
            collateral_balance=Decimal("1000"),
            allowance=Decimal("1000"),
        )
        self.submit_behavior: str = "accept"  # accept|reject|timeout|timeout_later
        self.cancel_behavior: str = "accept"  # accept|reject|timeout|not_found|already_filled
        self._submit_count = 0
        self._cancel_count = 0
        self._handlers: list[Callable] = []
        self.connected = True
        self.user_stream_ready = True
        self.submitted: list[SubmitOrderRequest] = []
        self.cancelled: list[str] = []
        self._pending_timeout_orders: dict[str, SubmitOrderRequest] = {}

    def submit_order(self, request: SubmitOrderRequest) -> SubmitOrderResult:
        self._submit_count += 1
        self.submitted.append(request)
        if not self.connected:
            return SubmitOrderResult(
                ok=False, venue_order_id=None, status=None, error="disconnected", uncertain=True
            )
        if self.submit_behavior == "reject":
            return SubmitOrderResult(
                ok=False,
                venue_order_id=None,
                status=None,
                error="INVALID_ORDER_MIN_SIZE",
                uncertain=False,
            )
        if self.submit_behavior in {"timeout", "timeout_later"}:
            # May have been accepted server-side without response.
            provisional = f"shadow-timeout-{self._submit_count}"
            if self.submit_behavior == "timeout_later":
                self._pending_timeout_orders[provisional] = request
            return SubmitOrderResult(
                ok=False,
                venue_order_id=None,
                status=None,
                error="timeout",
                uncertain=True,
            )
        vid = f"0xorder{self._submit_count:04d}"
        snap = VenueOrderSnapshot(
            venue_order_id=vid,
            status="live",
            instrument_token_id=request.token_id,
            side=request.side,
            original_size=Decimal(request.size),
            size_matched=Decimal("0"),
            price=Decimal(request.price),
        )
        self.open_orders[vid] = snap
        return SubmitOrderResult(
            ok=True, venue_order_id=vid, status="live", uncertain=False, raw={"orderID": vid}
        )

    def resolve_timeout_as_accepted(self, venue_order_id: str | None = None) -> str:
        """After uncertain submit, venue later shows the order (reconciliation path)."""
        req = next(iter(self._pending_timeout_orders.values()), None)
        if req is None and self.submitted:
            req = self.submitted[-1]
        assert req is not None
        vid = venue_order_id or f"0xrecovered{len(self.open_orders)+1:04d}"
        self.open_orders[vid] = VenueOrderSnapshot(
            venue_order_id=vid,
            status="live",
            instrument_token_id=req.token_id,
            side=req.side,
            original_size=Decimal(req.size),
            size_matched=Decimal("0"),
            price=Decimal(req.price),
        )
        self._pending_timeout_orders.clear()
        return vid

    def cancel_order(self, venue_order_id: str) -> CancelOrderResult:
        self._cancel_count += 1
        self.cancelled.append(venue_order_id)
        if self.cancel_behavior == "timeout":
            return CancelOrderResult(
                ok=False, venue_order_id=venue_order_id, error="timeout", uncertain=True
            )
        if self.cancel_behavior == "not_found":
            return CancelOrderResult(
                ok=False,
                venue_order_id=venue_order_id,
                error="order not found",
                already_terminal=False,
            )
        if self.cancel_behavior == "already_filled":
            return CancelOrderResult(
                ok=False,
                venue_order_id=venue_order_id,
                error="already filled",
                already_terminal=True,
            )
        if self.cancel_behavior == "reject":
            return CancelOrderResult(
                ok=False, venue_order_id=venue_order_id, error="cancel rejected"
            )
        self.open_orders.pop(venue_order_id, None)
        return CancelOrderResult(ok=True, venue_order_id=venue_order_id)

    def get_open_orders(self, *, market_id: str | None = None) -> list[VenueOrderSnapshot]:
        return list(self.open_orders.values())

    def get_order(self, venue_order_id: str) -> VenueOrderSnapshot | None:
        return self.open_orders.get(venue_order_id)

    def get_trades(
        self, *, market_id: str | None = None, after: str | None = None
    ) -> list[VenueTradeSnapshot]:
        return list(self.trades)

    def get_positions(self) -> list[VenuePositionSnapshot]:
        return list(self.positions)

    def get_balance(self) -> VenueBalanceSnapshot:
        return self.balance

    def subscribe_user_events(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._handlers.append(handler)

    def emit_user_event(self, payload: dict[str, Any]) -> None:
        for h in self._handlers:
            h(deepcopy(payload))

    def add_fill(
        self,
        *,
        venue_order_id: str,
        trade_id: str,
        token_id: str,
        side: str,
        size: str,
        price: str,
        status: str = "CONFIRMED",
    ) -> None:
        trade = VenueTradeSnapshot(
            venue_trade_id=trade_id,
            venue_order_id=venue_order_id,
            instrument_token_id=token_id,
            side=side,
            size=Decimal(size),
            price=Decimal(price),
            status=status,
            raw={"id": trade_id, "taker_order_id": venue_order_id, "status": status},
        )
        self.trades.append(trade)
        if venue_order_id in self.open_orders:
            o = self.open_orders[venue_order_id]
            matched = o.size_matched + Decimal(size)
            self.open_orders[venue_order_id] = VenueOrderSnapshot(
                venue_order_id=o.venue_order_id,
                status="matched" if matched >= o.original_size else "live",
                instrument_token_id=o.instrument_token_id,
                side=o.side,
                original_size=o.original_size,
                size_matched=matched,
                price=o.price,
                market_id=o.market_id,
                raw=dict(o.raw),
            )
            if matched >= o.original_size:
                del self.open_orders[venue_order_id]
        self.emit_user_event(
            {
                "event_type": "trade",
                "type": "TRADE",
                "id": trade_id,
                "asset_id": token_id,
                "side": side,
                "size": size,
                "price": price,
                "status": status,
                "taker_order_id": venue_order_id,
            }
        )

    def stop(self) -> None:
        self.connected = False
        self.user_stream_ready = False
        self._handlers.clear()
