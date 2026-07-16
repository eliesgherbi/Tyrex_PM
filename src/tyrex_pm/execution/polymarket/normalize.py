"""Normalize Polymarket user-channel / REST payloads to R5 execution events."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from tyrex_pm.core.events import EventSource
from tyrex_pm.core.execution_events import (
    ExecutionId,
    OrderAccepted,
    OrderCanceled,
    OrderFilled,
    OrderPartiallyFilled,
    OrderRejected,
    OrderSubmitted,
)
from tyrex_pm.core.ids import ClientOrderId, CorrelationId, InstrumentId, OrderId, new_event_id
from tyrex_pm.core.intents import OrderSide
from tyrex_pm.execution.polymarket.transport import VenueOrderSnapshot, VenueTradeSnapshot


def _ts(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if raw is None:
        return datetime.now(timezone.utc)
    if isinstance(raw, (int, float)):
        # ms or s heuristic
        v = float(raw)
        if v > 1e12:
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc)
    text = str(raw)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _side(raw: str) -> OrderSide:
    return OrderSide.BUY if str(raw).upper() in {"BUY", "0"} else OrderSide.SELL


def parse_venue_order_id(payload: Mapping[str, Any]) -> str | None:
    for key in ("orderID", "order_id", "id"):
        val = payload.get(key)
        if val:
            return str(val)
    return None


def venue_order_from_rest(row: Mapping[str, Any]) -> VenueOrderSnapshot:
    return VenueOrderSnapshot(
        venue_order_id=str(parse_venue_order_id(row) or ""),
        status=str(row.get("status") or ""),
        instrument_token_id=str(row.get("asset_id") or row.get("token_id") or ""),
        side=str(row.get("side") or ""),
        original_size=Decimal(str(row.get("original_size") or row.get("size") or "0")),
        size_matched=Decimal(str(row.get("size_matched") or "0")),
        price=Decimal(str(row.get("price") or "0")),
        market_id=None if row.get("market") is None else str(row.get("market")),
        raw=dict(row),
    )


def venue_trade_from_rest(row: Mapping[str, Any]) -> VenueTradeSnapshot:
    return VenueTradeSnapshot(
        venue_trade_id=str(row.get("id") or row.get("trade_id") or ""),
        venue_order_id=(
            None
            if row.get("taker_order_id") is None and row.get("order_id") is None
            else str(row.get("taker_order_id") or row.get("order_id"))
        ),
        instrument_token_id=str(row.get("asset_id") or ""),
        side=str(row.get("side") or ""),
        size=Decimal(str(row.get("size") or "0")),
        price=Decimal(str(row.get("price") or "0")),
        status=str(row.get("status") or ""),
        fee_rate_bps=(
            None
            if row.get("fee_rate_bps") is None
            else Decimal(str(row.get("fee_rate_bps")))
        ),
        market_id=None if row.get("market") is None else str(row.get("market")),
        raw=dict(row),
    )


def order_submitted_event(
    *,
    order_id: OrderId,
    client_order_id: ClientOrderId,
    instrument_id: InstrumentId,
    side: OrderSide,
    quantity: Decimal,
    limit_price: Decimal,
    correlation_id: CorrelationId,
    when: datetime,
) -> OrderSubmitted:
    return OrderSubmitted(
        event_id=new_event_id(),
        correlation_id=correlation_id,
        causation_id=None,
        ts_event=when,
        ts_received=when,
        source=EventSource.SYSTEM,
        order_id=order_id,
        client_order_id=client_order_id,
        instrument_id=instrument_id,
        side=side,
        quantity=quantity,
        limit_price=limit_price,
    )


def order_accepted_event(
    *,
    order_id: OrderId,
    venue_order_id: str,
    correlation_id: CorrelationId,
    when: datetime,
) -> OrderAccepted:
    return OrderAccepted(
        event_id=new_event_id(),
        correlation_id=correlation_id,
        causation_id=None,
        ts_event=when,
        ts_received=when,
        source=EventSource.SYSTEM,
        order_id=order_id,
        venue_order_id=venue_order_id,
    )


def order_rejected_event(
    *,
    order_id: OrderId,
    reason_code: str,
    correlation_id: CorrelationId,
    when: datetime,
) -> OrderRejected:
    return OrderRejected(
        event_id=new_event_id(),
        correlation_id=correlation_id,
        causation_id=None,
        ts_event=when,
        ts_received=when,
        source=EventSource.SYSTEM,
        order_id=order_id,
        reason_code=reason_code,
    )


def order_canceled_event(
    *,
    order_id: OrderId,
    reason_code: str,
    correlation_id: CorrelationId,
    when: datetime,
) -> OrderCanceled:
    return OrderCanceled(
        event_id=new_event_id(),
        correlation_id=correlation_id,
        causation_id=None,
        ts_event=when,
        ts_received=when,
        source=EventSource.SYSTEM,
        order_id=order_id,
        reason_code=reason_code,
    )


def fill_events_from_trade(
    *,
    order_id: OrderId,
    instrument_id: InstrumentId,
    trade: VenueTradeSnapshot,
    correlation_id: CorrelationId,
    cumulative_filled: Decimal,
    order_quantity: Decimal,
) -> OrderPartiallyFilled | OrderFilled:
    when = _ts(trade.raw.get("match_time") or trade.raw.get("timestamp"))
    rem = order_quantity - cumulative_filled
    if rem < 0:
        rem = Decimal("0")
    common = dict(
        event_id=new_event_id(),
        correlation_id=correlation_id,
        causation_id=None,
        ts_event=when,
        ts_received=datetime.now(timezone.utc),
        source=EventSource.SYSTEM,
        execution_id=ExecutionId(trade.venue_trade_id),
        order_id=order_id,
        instrument_id=instrument_id,
        side=_side(trade.side),
        fill_quantity=trade.size,
        fill_price=trade.price,
        fee_amount=Decimal("0"),
        fee_currency="USD",
        cumulative_filled=cumulative_filled,
        remaining_quantity=rem,
    )
    if rem == 0:
        return OrderFilled(**common)
    return OrderPartiallyFilled(**common)
