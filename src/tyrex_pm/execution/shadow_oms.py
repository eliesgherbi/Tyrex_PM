"""Deterministic ShadowOMS — simulated fills only; no venue submit.

Fill models:
- ``shadow_immediate_visible_depth_v0`` (F4/F5 default): match against the latest
  cached book at submit / book-update time (legacy).
- ``shadow_depth_walk_v1`` (N5): decision/plan time → configured latency → first
  recorded book available at or after simulated arrival → walk depth → optional
  extra slip ticks (never double-counted with depth impact).

Limitations (recorded in facts):
- No queue-position model
- No claim of venue-confirmed slippage or fees
- P&L remains simulated_shadow; fees estimated
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
from tyrex_pm.execution.shadow_fill_model import (
    FILL_MODEL_DEPTH_WALK_V1,
    FILL_MODEL_LEGACY_IMMEDIATE,
    DepthWalkAssumptions,
)
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
    # Fill model (distinct from fee model_id). Fixture-only values are not
    # production defaults — N5A uses shadow_depth_walk_v1 explicitly.
    fill_model_id: str = FILL_MODEL_LEGACY_IMMEDIATE
    latency_ms: float = 0.0
    extra_slip_ticks: Decimal = Decimal("0")
    tick_size: Decimal = Decimal("0.01")

    def assumptions(self) -> DepthWalkAssumptions:
        return DepthWalkAssumptions(
            fill_model_id=self.fill_model_id,
            latency_ms=self.latency_ms,
            extra_slip_ticks=self.extra_slip_ticks,
            tick_size=self.tick_size,
        )


@dataclass(frozen=True, kw_only=True)
class _BookObservation:
    """One recorded book with simulated availability (ingress/receive order)."""

    available_at: datetime
    book: BookSnapshot


@dataclass
class _MatchTrace:
    """Last match attempt metadata for facts / tests."""

    fill_model_id: str
    decision_plan_time: datetime | None = None
    simulated_arrival: datetime | None = None
    selected_book_available_at: datetime | None = None
    selected_book_ts_event: datetime | None = None
    latency_ms: float = 0.0
    extra_slip_ticks: Decimal = Decimal("0")
    filled_qty: Decimal = Decimal("0")
    residual_qty: Decimal = Decimal("0")
    outcome: str = "no_attempt"  # full | partial | no_fill | no_attempt


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
        self._book_history: dict[str, list[_BookObservation]] = {}
        self._book_ready = False
        self._pending_submit_match: set[str] = set()
        self._pending_residual_cancel: set[str] = set()
        self.last_match_trace: _MatchTrace | None = None
        self.match_traces: list[_MatchTrace] = []
        # Match after OrderStore applies ACCEPTED (handles reentrant publish queues).
        dispatcher.subscribe(OrderAccepted, self._on_accepted_match, priority=50)
        dispatcher.subscribe(
            OrderPartiallyFilled, self._on_fill_residual_policy, priority=40
        )
        dispatcher.subscribe(OrderFilled, self._on_fill_residual_policy, priority=40)

    @property
    def fee_model_id(self) -> str:
        return self._config.fee.model_id

    @property
    def fill_model_id(self) -> str:
        return self._config.fill_model_id

    def assumptions_fact(self) -> dict[str, object]:
        return self._config.assumptions().to_fact_dict()

    def stop(self) -> None:
        self._stopped = True

    def on_book_updated(
        self,
        book: BookSnapshot,
        *,
        available_at: datetime | None = None,
    ) -> None:
        """Record a book. ``available_at`` is ingress/receive time for depth-walk."""
        avail = available_at or book.ts_event
        inst = book.instrument_id.value
        self._books[inst] = book
        hist = self._book_history.setdefault(inst, [])
        hist.append(_BookObservation(available_at=avail, book=book))
        hist.sort(key=lambda o: (o.available_at, o.book.ts_event))
        self._book_ready = True
        if self._stopped:
            return
        self._reevaluate_working(trigger_instrument=inst)

    def invalidate_books(self) -> None:
        """Reconnect / recovery: block fills until a fresh book arrives."""
        self._books.clear()
        self._book_history.clear()
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

        if self._config.fill_model_id == FILL_MODEL_DEPTH_WALK_V1:
            self._try_match_depth_walk(order_id)
            return

        book = self._books.get(rec.instrument_id.value)
        if book is None:
            return
        # Legacy: do not fill from a book snapshot older than the order unless
        # this is the submit-time check or a fresh BookUpdated for this instrument.
        triggered = trigger_instrument == rec.instrument_id.value
        if not allow_pre_order_book and not triggered and book.ts_event < rec.created_at:
            return
        self._apply_fills_from_book(order_id, book, decision_plan_time=rec.created_at)

    def _try_match_depth_walk(self, order_id: OrderId) -> None:
        rec = self._orders.get(order_id)
        if rec is None:
            return
        decision_plan_time = rec.created_at
        arrival = decision_plan_time + timedelta(milliseconds=self._config.latency_ms)
        # Do not match before simulated arrival (latency). Use wall of the latest
        # recorded book as a proxy for "now" in offline replay.
        hist = self._book_history.get(rec.instrument_id.value) or []
        if not hist:
            return
        now_proxy = max(o.available_at for o in hist)
        if now_proxy < arrival:
            # Still waiting for simulated latency to elapse / later books.
            trace = _MatchTrace(
                fill_model_id=FILL_MODEL_DEPTH_WALK_V1,
                decision_plan_time=decision_plan_time,
                simulated_arrival=arrival,
                latency_ms=self._config.latency_ms,
                extra_slip_ticks=self._config.extra_slip_ticks,
                residual_qty=rec.remaining_quantity,
                outcome="no_fill",
            )
            self.last_match_trace = trace
            self.match_traces.append(trace)
            return
        # Book known at arrival: latest ingress observation with
        # available_at <= arrival. Never use a book that arrived after arrival
        # (no look-ahead). If none, wait for a book that is eligible.
        selected: _BookObservation | None = None
        for obs in hist:
            if obs.available_at <= arrival:
                selected = obs
            else:
                break
        trace = _MatchTrace(
            fill_model_id=FILL_MODEL_DEPTH_WALK_V1,
            decision_plan_time=decision_plan_time,
            simulated_arrival=arrival,
            latency_ms=self._config.latency_ms,
            extra_slip_ticks=self._config.extra_slip_ticks,
            residual_qty=rec.remaining_quantity,
            outcome="no_fill",
        )
        if selected is None:
            # No book was available at/before arrival yet — wait. A later book
            # with available_at<=arrival cannot appear after a later one, so
            # also accept the first book at/after arrival once latency elapsed
            # (ingress ordering): first obs with available_at >= arrival.
            for obs in hist:
                if obs.available_at >= arrival:
                    selected = obs
                    break
        if selected is None:
            self.last_match_trace = trace
            self.match_traces.append(trace)
            return
        # Reject look-ahead: never select a book whose availability is after
        # arrival when a pre-arrival book exists; already handled above.
        # If we fell through to first post-arrival book, that book is the first
        # eligible observation at/after arrival (N5 step 4).
        trace.selected_book_available_at = selected.available_at
        trace.selected_book_ts_event = selected.book.ts_event
        filled = self._apply_fills_from_book(
            order_id,
            selected.book,
            decision_plan_time=decision_plan_time,
            apply_extra_slip=True,
        )
        rec2 = self._orders.get(order_id)
        rem = Decimal("0") if rec2 is None else rec2.remaining_quantity
        if filled <= 0:
            trace.outcome = "no_fill"
            trace.filled_qty = Decimal("0")
            trace.residual_qty = rem
        elif rem <= 0:
            trace.outcome = "full"
            trace.filled_qty = filled
            trace.residual_qty = Decimal("0")
        else:
            trace.outcome = "partial"
            trace.filled_qty = filled
            trace.residual_qty = rem
        self.last_match_trace = trace
        self.match_traces.append(trace)

    def _apply_fills_from_book(
        self,
        order_id: OrderId,
        book: BookSnapshot,
        *,
        decision_plan_time: datetime,
        apply_extra_slip: bool = False,
    ) -> Decimal:
        rec = self._orders.get(order_id)
        if rec is None:
            return Decimal("0")
        remaining = rec.remaining_quantity
        if remaining <= 0:
            return Decimal("0")

        if rec.side is OrderSide.BUY:
            fills = self._match_buy(book, rec.limit_price, remaining)
        else:
            max_sell = self._portfolio.net_quantity(rec.instrument_id)
            sell_qty = min(remaining, max_sell)
            if sell_qty <= 0:
                return Decimal("0")
            fills = self._match_sell(book, rec.limit_price, sell_qty)

        if apply_extra_slip and self._config.extra_slip_ticks > 0 and fills:
            fills = self._apply_extra_slip(fills, side=rec.side)

        if not fills:
            return Decimal("0")

        now = book.ts_event if book.ts_event.tzinfo else datetime.now(timezone.utc)
        cum = rec.filled_quantity
        corr: CorrelationId = rec.correlation_id
        filled_total = Decimal("0")

        for price, qty in fills:
            cum += qty
            filled_total += qty
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
            self._pending_residual_cancel.add(order_id.value)
        _ = decision_plan_time
        return filled_total

    def _apply_extra_slip(
        self,
        fills: list[tuple[Decimal, Decimal]],
        *,
        side: OrderSide,
    ) -> list[tuple[Decimal, Decimal]]:
        """Worsen fill price by extra_slip_ticks — not double-counted in depth walk."""
        slip = self._config.extra_slip_ticks * self._config.tick_size
        out: list[tuple[Decimal, Decimal]] = []
        for price, qty in fills:
            if side is OrderSide.BUY:
                out.append((price + slip, qty))
            else:
                out.append((max(Decimal("0"), price - slip), qty))
        return out

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
