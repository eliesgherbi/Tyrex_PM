"""Deterministic ShadowOMS — visible-depth fills only; no venue submit.

Limitations (recorded in facts):
- No queue-position model
- No latency / market-impact model
- Passive against visible book depth only
- Shadow performance is not profitability evidence
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from tyrex_pm.core.commands import CancelOrderCommand, SubmitOrderCommand, new_command_id
from tyrex_pm.core.events import EventSource
from tyrex_pm.core.execution_events import (
    ExecutionId,
    OrderAccepted,
    OrderCanceled,
    OrderCancelPending,
    OrderFilled,
    OrderPartiallyFilled,
    OrderSubmitted,
)
from tyrex_pm.core.ids import CorrelationId, OrderId, new_event_id, new_order_id
from tyrex_pm.core.intents import OrderSide
from tyrex_pm.core.snapshots import BookSnapshot
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.execution.order_store import OrderStatus, OrderStore
from tyrex_pm.portfolio.portfolio import Portfolio


@dataclass(frozen=True, kw_only=True)
class ShadowFeeConfig:
    """Explicit shadow fee model — not Z-Gap fd fees."""

    fee_rate: Decimal = Decimal("0")
    fee_currency: str = "USD"
    model_id: str = "shadow_zero_fee_v1"

    def fee_for(self, notional: Decimal) -> Decimal:
        return (notional * self.fee_rate).quantize(Decimal("0.000001"))


@dataclass(frozen=True, kw_only=True)
class ShadowFillConfig:
    cancel_unfilled_residual: bool = False
    fee: ShadowFeeConfig = ShadowFeeConfig()


class ShadowOMS:
    """Implements OMS protocol with deterministic visible-depth matching."""

    def __init__(
        self,
        *,
        dispatcher: EventDispatcher,
        order_store: OrderStore,
        portfolio: Portfolio,
        config: ShadowFillConfig | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._orders = order_store
        self._portfolio = portfolio
        self._config = config or ShadowFillConfig()
        self._stopped = False
        self._books: dict[str, BookSnapshot] = {}
        self._book_ready = False
        self._pending_submit_match: set[str] = set()
        self._pending_residual_cancel: set[str] = set()
        # Match after OrderStore applies ACCEPTED (handles reentrant publish queues).
        dispatcher.subscribe(OrderAccepted, self._on_accepted_match, priority=50)
        dispatcher.subscribe(
            OrderPartiallyFilled, self._on_fill_residual_policy, priority=40
        )
        dispatcher.subscribe(OrderFilled, self._on_fill_residual_policy, priority=40)

    @property
    def fee_model_id(self) -> str:
        return self._config.fee.model_id

    def stop(self) -> None:
        self._stopped = True

    def on_book_updated(self, book: BookSnapshot) -> None:
        self._books[book.instrument_id.value] = book
        self._book_ready = True
        if self._stopped:
            return
        self._reevaluate_working(trigger_instrument=book.instrument_id.value)

    def invalidate_books(self) -> None:
        """Reconnect / recovery: block fills until a fresh book arrives."""
        self._books.clear()
        self._book_ready = False

    def _on_accepted_match(self, event: OrderAccepted) -> None:
        if event.order_id.value not in self._pending_submit_match:
            return
        self._pending_submit_match.discard(event.order_id.value)
        self._try_match(event.order_id, allow_pre_order_book=True)

    def _on_fill_residual_policy(
        self, event: OrderPartiallyFilled | OrderFilled
    ) -> None:
        oid = event.order_id.value
        if oid not in self._pending_residual_cancel:
            return
        rec = self._orders.get(event.order_id)
        if rec is None:
            return
        if rec.remaining_quantity <= 0 or rec.status is OrderStatus.FILLED:
            self._pending_residual_cancel.discard(oid)
            return
        if rec.status is OrderStatus.PARTIALLY_FILLED and self._config.cancel_unfilled_residual:
            self._pending_residual_cancel.discard(oid)
            self.cancel(
                CancelOrderCommand(
                    command_id=new_command_id(),
                    order_id=event.order_id,
                    reason_code="SHADOW_CANCEL_RESIDUAL",
                    created_at=event.ts_event,
                    correlation_id=rec.correlation_id,
                    causation_id=None,
                )
            )

    def submit(self, command: SubmitOrderCommand, *, order_id: OrderId | None = None) -> OrderId:
        if self._stopped:
            raise RuntimeError("ShadowOMS stopped")
        order_id = order_id or new_order_id()
        self._orders.create_from_command(command, order_id=order_id)
        self._pending_submit_match.add(order_id.value)
        now = command.created_at
        self._publish(
            OrderSubmitted(
                event_id=new_event_id(),
                correlation_id=command.correlation_id,
                causation_id=command.causation_id,
                ts_event=now,
                ts_received=now,
                source=EventSource.SYSTEM,
                order_id=order_id,
                client_order_id=command.client_order_id,
                instrument_id=command.instrument_id,
                side=command.side,
                quantity=command.quantity,
                limit_price=command.limit_price,
            )
        )
        self._publish(
            OrderAccepted(
                event_id=new_event_id(),
                correlation_id=command.correlation_id,
                causation_id=command.causation_id,
                ts_event=now,
                ts_received=now,
                source=EventSource.SYSTEM,
                order_id=order_id,
                venue_order_id=f"shadow-{order_id.value}",
            )
        )
        return order_id

    def cancel(self, command: CancelOrderCommand) -> None:
        rec = self._orders.get(command.order_id)
        if rec is None:
            return
        if rec.status in {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}:
            return
        now = command.created_at
        self._publish(
            OrderCancelPending(
                event_id=new_event_id(),
                correlation_id=command.correlation_id,
                causation_id=command.causation_id,
                ts_event=now,
                ts_received=now,
                source=EventSource.SYSTEM,
                order_id=command.order_id,
            )
        )
        self._publish(
            OrderCanceled(
                event_id=new_event_id(),
                correlation_id=command.correlation_id,
                causation_id=command.causation_id,
                ts_event=now,
                ts_received=now,
                source=EventSource.SYSTEM,
                order_id=command.order_id,
                reason_code=command.reason_code,
            )
        )

    def _reevaluate_working(self, *, trigger_instrument: str | None = None) -> None:
        for rec in list(self._orders.working_orders()):
            if rec.status in {OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED}:
                self._try_match(
                    rec.order_id,
                    allow_pre_order_book=False,
                    trigger_instrument=trigger_instrument,
                )

    def _try_match(
        self,
        order_id: OrderId,
        *,
        allow_pre_order_book: bool = False,
        trigger_instrument: str | None = None,
    ) -> None:
        if not self._book_ready:
            return
        rec = self._orders.get(order_id)
        if rec is None:
            return
        if rec.status not in {OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED}:
            return
        book = self._books.get(rec.instrument_id.value)
        if book is None:
            return
        # Do not fill from a book snapshot older than the order unless this is
        # the submit-time check or a fresh BookUpdated for this instrument.
        triggered = trigger_instrument == rec.instrument_id.value
        if not allow_pre_order_book and not triggered and book.ts_event < rec.created_at:
            return

        remaining = rec.remaining_quantity
        if remaining <= 0:
            return

        if rec.side is OrderSide.BUY:
            fills = self._match_buy(book, rec.limit_price, remaining)
        else:
            max_sell = self._portfolio.net_quantity(rec.instrument_id)
            sell_qty = min(remaining, max_sell)
            if sell_qty <= 0:
                return
            fills = self._match_sell(book, rec.limit_price, sell_qty)

        if not fills:
            return

        # Deterministic event time: prefer book event time over wall clock.
        now = book.ts_event if book.ts_event.tzinfo else datetime.now(timezone.utc)
        cum = rec.filled_quantity
        corr: CorrelationId = rec.correlation_id

        for price, qty in fills:
            cum += qty
            rem = rec.quantity - cum
            fee = self._config.fee.fee_for(price * qty)
            eid = ExecutionId(str(uuid4()))
            common = dict(
                event_id=new_event_id(),
                correlation_id=corr,
                causation_id=None,
                ts_event=now,
                ts_received=now,
                source=EventSource.SYSTEM,
                execution_id=eid,
                order_id=order_id,
                instrument_id=rec.instrument_id,
                side=rec.side,
                fill_quantity=qty,
                fill_price=price,
                fee_amount=fee,
                fee_currency=self._config.fee.fee_currency,
                cumulative_filled=cum,
                remaining_quantity=rem,
            )
            if rem == 0:
                self._publish(OrderFilled(**common))
            else:
                self._publish(OrderPartiallyFilled(**common))

        if self._config.cancel_unfilled_residual and fills:
            # Cancel residual only after OrderStore applies fill totals.
            self._pending_residual_cancel.add(order_id.value)

    def _match_buy(
        self, book: BookSnapshot, limit: Decimal, quantity: Decimal
    ) -> list[tuple[Decimal, Decimal]]:
        out: list[tuple[Decimal, Decimal]] = []
        left = quantity
        for level in book.asks:
            if level.price > limit:
                break
            take = min(left, level.quantity)
            if take > 0:
                out.append((level.price, take))
                left -= take
            if left <= 0:
                break
        return out

    def _match_sell(
        self, book: BookSnapshot, limit: Decimal, quantity: Decimal
    ) -> list[tuple[Decimal, Decimal]]:
        out: list[tuple[Decimal, Decimal]] = []
        left = quantity
        for level in book.bids:
            if level.price < limit:
                break
            take = min(left, level.quantity)
            if take > 0:
                out.append((level.price, take))
                left -= take
            if left <= 0:
                break
        return out

    def _publish(self, event) -> None:
        self._dispatcher.publish(event)
