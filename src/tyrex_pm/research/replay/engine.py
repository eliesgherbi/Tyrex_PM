"""Exact cash/inventory simulation under explicitly versioned execution assumptions."""

from __future__ import annotations

import heapq
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any

from tyrex_pm.data_warehouse.contracts import exact
from tyrex_pm.data_warehouse.reconstruction.books import Books
from tyrex_pm.domain.polymarket.fees import FeeCurveParams, phi_taker_fee_per_share

from .config import Intent, Scenario

# Closed-loop timeline priorities at one nanosecond (research-to-live plan, section 7).
MARKET_OBSERVATION = 0
ORDER_ARRIVAL = 1
EXECUTION_FEEDBACK = 2
STRATEGY_OBSERVATION = 3
TIMER = 4
DECISION = 5


@dataclass
class Order:
    intent: Intent
    remaining: Decimal
    arrived_at: int
    queue_ahead: Decimal = Decimal(0)
    active: bool = True


class Engine:
    def __init__(self, scenario: Scenario):
        scenario.validate()
        self.scenario = scenario
        self.books = Books()
        self.cash = exact(scenario.initial_cash)
        self.inventory: dict[str, Decimal] = {}
        self.cost_basis: dict[str, Decimal] = {}
        self.realized = Decimal(0)
        self.orders: dict[str, Order] = {}
        self.ledger: list[dict] = []
        self.invalid_reasons: set[str] = set()
        self.consumed: dict[tuple, Decimal] = {}
        self.constraint: dict[str, dict] = {}
        self.market_by_instrument: dict[str, str] = {}
        self.metadata_by_market: dict[str, dict] = {}
        self.curves: dict[str, FeeCurveParams] = {}
        self.fees_paid = Decimal(0)
        self.now = 0
        self.curve = FeeCurveParams(
            fee_rate=exact(scenario.fee_policy.rate), exponent=Decimal(scenario.fee_policy.exponent)
        )
        self.participation = exact(scenario.participation)
        self._timeline: list | None = None
        self._serial = 0

    def record(self, event: str, **values: Any) -> None:
        self.ledger.append({"at_ns": self.now, "event": event, **values})

    def reservations(self, exclude: str | None = None) -> tuple[Decimal, dict[str, Decimal]]:
        cash, positions = Decimal(0), {}
        for oid, order in self.orders.items():
            if oid == exclude or not order.active:
                continue
            intent = order.intent
            if intent.action == "BUY":
                unit = exact(intent.limit_price)
                if self.scenario.fee_policy.buy_fee_unit == "cash":
                    unit += self.curves.get(intent.instrument_id, self.curve).fee_rate
                cash += order.remaining * unit
            else:
                positions[intent.instrument_id] = (
                    positions.get(intent.instrument_id, Decimal(0)) + order.remaining
                )
        return cash, positions

    def reject(self, intent: Intent, reason: str) -> None:
        self.record("rejected", order_id=intent.order_id, reason=reason)

    def submit(self, intent: Intent) -> None:
        intent.validate()
        if intent.action == "CANCEL":
            order = self.orders.get(intent.order_id)
            if order and order.active:
                order.active = False
                self.record("cancelled", order_id=intent.order_id, unfilled=order.remaining)
            else:
                self.record("cancel_no_active_order", order_id=intent.order_id)
            return
        if intent.order_id in self.orders:
            self.reject(intent, "duplicate_order_id")
            return
        book = self.books.books.get(intent.instrument_id)
        if book is None or not book.valid:
            self.invalid_reasons.add("invalid_book_at_action")
            self.reject(intent, "invalid_book")
            return
        limit, quantity = exact(intent.limit_price), exact(intent.quantity)
        if self.scenario.fee_policy.mode == "recorded" and intent.instrument_id not in self.curves:
            self.invalid_reasons.add("recorded_fee_curve_unavailable")
            self.reject(intent, "recorded_fee_curve_unavailable")
            return
        constraints = self.constraint.get(intent.instrument_id, {})
        tick, minimum = constraints.get("tick"), constraints.get("minimum_shares")
        if tick is None or minimum is None:
            self.invalid_reasons.add("order_constraints_unqualified")
        if tick is not None and limit % tick:
            self.reject(intent, "off_tick")
            return
        if minimum is not None and quantity < minimum:
            self.reject(intent, "below_minimum")
            return
        reserved, positions = self.reservations()
        reserve_unit = limit + (
            self.curves.get(intent.instrument_id, self.curve).fee_rate
            if self.scenario.fee_policy.buy_fee_unit == "cash"
            else 0
        )
        if intent.action == "BUY" and quantity * reserve_unit > self.cash - reserved:
            self.reject(intent, "insufficient_available_cash")
            return
        if intent.action == "SELL" and quantity > self.inventory.get(
            intent.instrument_id, Decimal(0)
        ) - positions.get(intent.instrument_id, Decimal(0)):
            self.reject(intent, "insufficient_unreserved_inventory")
            return
        side = book.asks if intent.action == "BUY" else book.bids
        prices = sorted(side, reverse=intent.action == "SELL")
        # Friction scenario: each fill pays this many ticks worse than the displayed level,
        # never beyond the order's limit.
        adverse = Decimal(0)
        if self.scenario.adverse_price_ticks:
            if tick is None:
                self.reject(intent, "adverse_ticks_without_tick")
                return
            adverse = tick * self.scenario.adverse_price_ticks
        if intent.action == "BUY":
            eligible = [p for p in prices if p + adverse <= limit]
        else:
            eligible = [p for p in prices if p - adverse >= limit]
        if intent.post_only and eligible:
            self.reject(intent, "post_only_would_cross")
            return
        capacity = {}
        for price in eligible:
            key = (book.epoch_id, intent.action, price)
            capacity[price] = max(
                Decimal(0), side[price] * self.participation - self.consumed.get(key, Decimal(0))
            )
        if intent.lifetime == "FOK" and sum(capacity.values()) < quantity:
            self.reject(intent, "fok_insufficient_depth")
            return
        order = Order(intent, quantity, self.now)
        self.orders[intent.order_id] = order
        self.record("arrived", order_id=intent.order_id, intent=asdict(intent))
        for price in eligible:
            take = min(order.remaining, capacity[price])
            if take:
                paid = price + adverse if intent.action == "BUY" else price - adverse
                self.fill(order, take, paid, maker=False)
                key = (book.epoch_id, intent.action, price)
                self.consumed[key] = self.consumed.get(key, Decimal(0)) + take
        if order.remaining and intent.lifetime == "GTC":
            own_side = book.bids if intent.action == "BUY" else book.asks
            order.queue_ahead = own_side.get(limit, Decimal(0)) * exact(
                self.scenario.maker_queue_multiplier
            )
            self.record("resting", order_id=intent.order_id, queue_ahead=order.queue_ahead)
        else:
            order.active = False
            if order.remaining:
                self.record(
                    "unfilled_cancelled", order_id=intent.order_id, quantity=order.remaining
                )

    def fill(self, order: Order, quantity: Decimal, price: Decimal, *, maker: bool) -> None:
        curve = self.curves.get(order.intent.instrument_id, self.curve)
        fee = Decimal(0) if maker else (quantity * phi_taker_fee_per_share(price, curve))
        quantum = exact(self.scenario.fee_policy.quantum)
        fee = (fee / quantum).quantize(Decimal(1), rounding=ROUND_HALF_UP) * quantum
        cost = quantity * price
        iid = order.intent.instrument_id
        held = self.inventory.get(iid, Decimal(0))
        basis = self.cost_basis.get(iid, Decimal(0))
        net_quantity = quantity
        if order.intent.action == "BUY":
            if self.scenario.fee_policy.buy_fee_unit == "shares":
                net_quantity -= fee / price
            else:
                cost += fee
            if net_quantity < 0 or cost > self.cash:
                raise ValueError("invalid simulated buy debit")
            self.cash -= cost
            self.inventory[iid] = held + net_quantity
            self.cost_basis[iid] = basis + cost
        else:
            if quantity > held:
                raise ValueError("simulation oversold inventory")
            released_basis = basis * quantity / held
            self.cash += cost - fee
            self.inventory[iid] = held - quantity
            self.cost_basis[iid] = basis - released_basis
            self.realized += cost - fee - released_basis
        order.remaining -= quantity
        if order.remaining == 0:
            order.active = False
        self.fees_paid += fee
        self.record(
            "fill",
            order_id=order.intent.order_id,
            instrument_id=iid,
            side=order.intent.action,
            quantity=quantity,
            net_quantity=net_quantity,
            price=price,
            fee=fee,
            maker=maker,
            cash=self.cash,
            inventory=self.inventory[iid],
            model=self.scenario.maker_model if maker else "depth_participation_v1",
        )

    def observation(self, dataset: str, row: dict) -> None:
        if dataset == "instruments" and row.get("market_id"):
            iid, mid = row["instrument_id"], row["market_id"]
            self.market_by_instrument[iid] = mid
            if mid in self.metadata_by_market:
                self.apply_metadata(iid, self.metadata_by_market[mid])
        if dataset == "metadata_versions" and row.get("entity_type") == "market":
            mid = row["entity_id"]
            previous = self.metadata_by_market.get(mid, {})
            # A later source with absent fields does not erase already observed constraints.
            merged = {**previous, **{k: v for k, v in row.items() if v is not None}}
            if row.get("fee_schedule") in (None, "null"):
                merged["fee_schedule"] = previous.get("fee_schedule")
            self.metadata_by_market[mid] = merged
            for iid, market in self.market_by_instrument.items():
                if market == mid:
                    self.apply_metadata(iid, merged)
        previously_valid = {iid for iid, book in self.books.books.items() if book.valid}
        self.books.apply(dataset, row)
        for iid in previously_valid:
            if not self.books.books[iid].valid and (
                self.inventory.get(iid, 0)
                or any(o.active and o.intent.instrument_id == iid for o in self.orders.values())
            ):
                self.invalid_reasons.add("gap_with_exposure")
        if dataset == "market_changes" and row.get("change_type") == "tick_size_change":
            from tyrex_pm.data_warehouse.contracts import decode

            value = decode(row["new_value"])
            self.constraint.setdefault(row["entity_id"], {})["tick"] = exact(value["new_tick_size"])
        if dataset != "trade_prints" or self.scenario.maker_model == "no_fill":
            return
        # This side interpretation is explicitly an assumption, never inferred private queue data.
        remaining = row.get("quantity")
        if remaining is None or row.get("reported_side") not in ("BUY", "SELL"):
            return
        remaining *= self.participation
        for order in sorted(self.orders.values(), key=lambda o: (o.arrived_at, o.intent.order_id)):
            intent = order.intent
            if not order.active or intent.instrument_id != row["instrument_id"]:
                continue
            if row["reported_side"] == intent.action or row["price"] != exact(intent.limit_price):
                continue
            book = self.books.books.get(intent.instrument_id)
            if book is None or not book.valid:
                continue
            ahead = min(order.queue_ahead, remaining)
            order.queue_ahead -= ahead
            remaining -= ahead
            amount = min(order.remaining, remaining)
            if amount > 0:
                self.fill(order, amount, exact(intent.limit_price), maker=True)
                remaining -= amount

    def apply_metadata(self, iid: str, row: dict) -> None:
        from tyrex_pm.data_warehouse.contracts import decode

        constraints = self.constraint.setdefault(iid, {})
        if row.get("tick_size") is not None:
            constraints["tick"] = row["tick_size"]
        if row.get("minimum_order_size_unit") == "shares":
            constraints["minimum_shares"] = row["minimum_order_size_value"]
        schedule = decode(row.get("fee_schedule") or "null")
        if not isinstance(schedule, dict):
            return
        # Two recorded shapes carry the same curve: CLOB {"r", "e"} and Gamma
        # {"rate", "exponent"}. Anything else is not interpreted.
        if "r" in schedule and "e" in schedule:
            rate, exponent = exact(schedule["r"]), exact(schedule["e"])
        elif "rate" in schedule and "exponent" in schedule:
            rate, exponent = exact(schedule["rate"]), exact(schedule["exponent"])
        else:
            if self.scenario.fee_policy.mode == "recorded":
                self.curves.pop(iid, None)
            return
        if exponent != exponent.to_integral_value():
            self.curves.pop(iid, None)
            self.invalid_reasons.add("fractional_fee_exponent_unsupported")
            return
        if self.scenario.fee_policy.mode == "recorded":
            curve = FeeCurveParams(fee_rate=rate, exponent=exponent)
            if self.curves.get(iid) != curve:
                self.curves[iid] = curve
                self.record(
                    "fee_curve_changed",
                    instrument_id=iid,
                    rate=rate,
                    exponent=exponent,
                    metadata_record_id=row.get("record_id"),
                )

    def _settle(
        self, labels: list[dict] | None, accepted_statuses: tuple[str, ...]
    ) -> tuple[dict[str, Decimal], Decimal, list[str]]:
        receivables = {}
        settled = set()
        for label in sorted(labels or [], key=lambda r: r["available_at"]):
            iid = label["instrument_id"]
            if iid in settled or label["status"] not in accepted_statuses:
                continue
            payout = exact(label["payout"])
            if not 0 <= payout <= 1:
                raise ValueError("invalid payout")
            settled.add(iid)
            quantity = self.inventory.get(iid, Decimal(0))
            if quantity == 0:
                continue
            receivables[iid] = quantity * payout
            self.realized += quantity * payout - self.cost_basis.get(iid, Decimal(0))
            # Retrospective labels never finance earlier actions.
            status = label["status"]
            extra = {} if status == "PROVIDER_TERMINAL" else {"label_status": status}
            self.record(
                "payout_receivable",
                instrument_id=iid,
                amount=receivables[iid],
                available_at=label["available_at"],
                cash_release_at=label["available_at"]
                + self.scenario.settlement_release_delay_ms * 10**6,
                **extra,
            )
        marked = Decimal(0)
        unmarked = []
        for iid, quantity in self.inventory.items():
            if iid in settled or quantity == 0:
                continue
            book = self.books.books.get(iid)
            if book and book.valid and book.bids:
                marked += quantity * max(book.bids)
            else:
                unmarked.append(iid)
        return receivables, marked, unmarked

    def run(
        self,
        events: list[tuple[str, dict]],
        intents: list[Intent],
        labels: list[dict] | None = None,
    ) -> dict:
        queue = []
        serial = 0
        horizon = max((row["received_at"] for _, row in events), default=0)
        for dataset, row in events:
            serial += 1
            # Recorded book is the arrival-time proxy. Extra feed lag delays decisions,
            # not the market itself: otherwise a delayed strategy could fill at old prices.
            heapq.heappush(
                queue,
                (
                    row["received_at"],
                    0,
                    serial,
                    "observation",
                    (dataset, row),
                ),
            )
        for intent in intents:
            intent.validate()
            serial += 1
            delay = (
                self.scenario.cancel_delay_ms
                if intent.action == "CANCEL"
                else self.scenario.order_delay_ms
            )
            at = (
                intent.at_ns
                + (self.scenario.feed_delay_ms + self.scenario.decision_delay_ms + delay) * 10**6
            )
            heapq.heappush(queue, (at, 1, serial, "intent", intent))
        with localcontext() as ctx:
            ctx.prec = 90
            while queue:
                self.now, _, _, kind, payload = heapq.heappop(queue)
                if kind == "observation":
                    self.observation(*payload)
                elif self.now > horizon:
                    self.reject(payload, "arrival_outside_recording")
                    self.invalid_reasons.add("arrival_outside_recording")
                else:
                    self.submit(payload)
            receivables, marked, unmarked = self._settle(labels, ("PROVIDER_TERMINAL",))
        return self._result(receivables, marked, unmarked)

    def _result(
        self, receivables: dict[str, Decimal], marked: Decimal, unmarked: list[str]
    ) -> dict:
        return {
            "scenario": asdict(self.scenario),
            "cash": self.cash,
            "inventory": self.inventory,
            "payout_receivables": receivables,
            "unrealized_mark_value": marked,
            "unmarked_instruments": unmarked,
            "realized_pnl": self.realized,
            "fees_paid": self.fees_paid,
            "ledger": self.ledger,
            "net_equity_change": self.cash
            + sum(receivables.values())
            + marked
            - exact(self.scenario.initial_cash)
            if not unmarked
            else None,
            "qualified": not self.invalid_reasons,
            "limitations": sorted(self.invalid_reasons),
            "fee_provenance": self.scenario.fee_policy.provenance,
            "maker_fills_are_scenario_conditional": True,
            "liquidity_replenishment_policy": "no_reuse_within_book_epoch",
            "mark_is_executable_liquidation": False,
        }

    # -- closed-loop strategy replay -------------------------------------------------

    def schedule(self, at_ns: int, priority: int, kind: str, payload: Any) -> None:
        """Push one event onto the closed-loop timeline (``run_closed_loop`` only)."""
        if self._timeline is None:
            raise RuntimeError("schedule() is only valid during run_closed_loop")
        if at_ns < self.now:
            raise ValueError("cannot schedule an event in the past")
        self._serial += 1
        heapq.heappush(self._timeline, (at_ns, priority, self._serial, kind, payload))

    def schedule_order(self, intent: Intent, *, decided_at_ns: int) -> int:
        """Schedule an order decided at ``decided_at_ns``; returns its arrival time.

        Delays: decision (compute) then order (submission to venue). Information delay
        is applied earlier, to the strategy's view of the data, never to the market.
        """
        intent.validate()
        delay = (
            self.scenario.cancel_delay_ms
            if intent.action == "CANCEL"
            else self.scenario.order_delay_ms
        )
        at = decided_at_ns + (self.scenario.decision_delay_ms + delay) * 10**6
        self.schedule(at, ORDER_ARRIVAL, "order", intent)
        return at

    def run_closed_loop(
        self,
        events: list[tuple[str, dict]],
        agent: Any,
        labels: list[dict] | None = None,
        *,
        accepted_label_statuses: tuple[str, ...] = ("PROVIDER_TERMINAL",),
    ) -> dict:
        """One merged timeline of market data, strategy views, timers, orders and feedback.

        ``agent`` provides ``strategy_datasets`` (datasets the strategy sees),
        ``start(engine, horizon_ns)`` and ``handle(kind, payload)`` for kinds other than
        market observations and orders. Ties at one nanosecond follow the priority
        constants: market data, order arrival, feedback, strategy data, timer, decision.
        """
        self._timeline = []
        self._serial = 0
        horizon = max((row["received_at"] for _, row in events), default=0)
        info_delay = self.scenario.feed_delay_ms * 10**6
        wanted = set(agent.strategy_datasets)
        for dataset, row in events:
            self._serial += 1
            heapq.heappush(
                self._timeline,
                (
                    row["received_at"],
                    MARKET_OBSERVATION,
                    self._serial,
                    "observation",
                    (dataset, row),
                ),
            )
            if dataset in wanted:
                self._serial += 1
                heapq.heappush(
                    self._timeline,
                    (
                        row["received_at"] + info_delay,
                        STRATEGY_OBSERVATION,
                        self._serial,
                        "strategy_observation",
                        (dataset, row),
                    ),
                )
        try:
            with localcontext() as ctx:
                ctx.prec = 90
                agent.start(self, horizon)
                while self._timeline:
                    self.now, _, _, kind, payload = heapq.heappop(self._timeline)
                    if kind == "observation":
                        self.observation(*payload)
                    elif kind == "order":
                        self._closed_loop_order(payload, horizon)
                    else:
                        agent.handle(kind, payload)
                receivables, marked, unmarked = self._settle(labels, accepted_label_statuses)
        finally:
            self._timeline = None
        return self._result(receivables, marked, unmarked)

    def _closed_loop_order(self, intent: Intent, horizon: int) -> None:
        start = len(self.ledger)
        if self.now > horizon:
            self.reject(intent, "arrival_outside_recording")
            self.invalid_reasons.add("arrival_outside_recording")
        else:
            self.submit(intent)
        entries = [e for e in self.ledger[start:] if e.get("order_id") == intent.order_id]
        self.schedule(
            self.now + self.scenario.feedback_delay_ms * 10**6,
            EXECUTION_FEEDBACK,
            "feedback",
            {"order_id": intent.order_id, "entries": entries},
        )
