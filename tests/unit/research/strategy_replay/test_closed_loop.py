"""Closed-loop timeline: chronology, arrival-time books, timers, feedback, friction."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from tyrex_pm.research.replay.config import FeePolicy, Intent, Scenario
from tyrex_pm.research.replay.engine import DECISION, TIMER, Engine

IID = "polymarket:137:outcome:1"
MS = 1_000_000


def book(at, ask="0.50", quantity="10", bid="0.40"):
    return "book_snapshots", {
        "instrument_id": IID,
        "record_id": f"b{at}",
        "received_at": at,
        "source_id": "clob",
        "session_id": "s",
        "connection_id": "c",
        "snapshot_origin": "websocket",
        "bids": [{"price": Decimal(bid), "quantity": Decimal("5")}],
        "asks": [{"price": Decimal(ask), "quantity": Decimal(quantity)}],
        "provider_update_id": None,
    }


def engine(**changes) -> Engine:
    settings = {
        "adapter": "strategy_closed_loop_v1",
        "order_delay_ms": 0,
        "cancel_delay_ms": 0,
        "participation": "1",
        "fee_policy": FeePolicy(rate="0"),
        **changes,
    }
    scenario = replace(Scenario(), **settings)
    result = Engine(scenario)
    result.constraint[IID] = {"tick": Decimal("0.01"), "minimum_shares": Decimal("1")}
    return result


class Agent:
    """Minimal scripted agent: records what it saw and when, places scripted orders."""

    strategy_datasets = ("book_snapshots",)

    def __init__(self, orders=(), timer_ms=None):
        self.orders = list(orders)  # (decide_at_ns, Intent)
        self.timer_ms = timer_ms
        self.seen: list[tuple[str, int, int | None]] = []
        self.feedback: list[tuple[int, dict]] = []

    def start(self, engine, horizon):
        self.engine, self.horizon = engine, horizon
        for at, _ in self.orders:
            engine.schedule(at, DECISION, "decision", at)
        if self.timer_ms:
            engine.schedule(0, TIMER, "timer", None)

    def handle(self, kind, payload):
        now = self.engine.now
        if kind == "strategy_observation":
            self.seen.append(("obs", now, payload[1]["received_at"]))
        elif kind == "timer":
            self.seen.append(("timer", now, None))
            if now + self.timer_ms * MS <= self.horizon:
                self.engine.schedule(now + self.timer_ms * MS, TIMER, "timer", None)
        elif kind == "decision":
            for at, intent in self.orders:
                if at == payload:
                    self.engine.schedule_order(intent, decided_at_ns=now)
        elif kind == "feedback":
            self.feedback.append((now, payload))


def intent(oid="a", q="2", price="0.50"):
    return Intent(oid, 0, IID, "BUY", q, price)


def fills(result):
    return [e for e in result["ledger"] if e["event"] == "fill"]


def test_strategy_sees_rows_only_after_information_delay() -> None:
    agent = Agent()
    engine(feed_delay_ms=100).run_closed_loop([book(0), book(50 * MS)], agent)
    assert [(at, received) for _, at, received in agent.seen] == [
        (100 * MS, 0),
        (150 * MS, 50 * MS),
    ]


def test_order_arriving_between_updates_uses_book_at_arrival() -> None:
    # Decided at 10 ms, arrives at 310 ms: after the 200 ms update (ask 0.55), before 400 ms.
    agent = Agent(orders=[(10 * MS, intent(price="0.60"))])
    result = engine(order_delay_ms=300).run_closed_loop(
        [book(0, ask="0.50"), book(200 * MS, ask="0.55"), book(400 * MS, ask="0.70")], agent
    )
    (fill,) = fills(result)
    assert fill["price"] == Decimal("0.55") and fill["at_ns"] == 310 * MS


def test_timers_fire_during_quiet_periods() -> None:
    agent = Agent(timer_ms=1000)
    engine().run_closed_loop([book(0), book(5000 * MS)], agent)
    timers = [at for kind, at, _ in agent.seen if kind == "timer"]
    assert timers == [i * 1000 * MS for i in range(6)]


def test_feedback_is_delivered_after_its_delay() -> None:
    agent = Agent(orders=[(10 * MS, intent())])
    engine(order_delay_ms=300, feedback_delay_ms=300).run_closed_loop(
        [book(0), book(2000 * MS)], agent
    )
    ((at, payload),) = agent.feedback
    assert at == 610 * MS
    assert [e["event"] for e in payload["entries"]] == ["arrived", "fill"]


def test_same_nanosecond_order_market_data_first_then_strategy_then_decision() -> None:
    order: list[str] = []

    class Tie(Agent):
        def start(self, engine, horizon):
            super().start(engine, horizon)
            engine.schedule(0, DECISION, "decision", -1)
            engine.schedule(0, TIMER, "timer", None)

        def handle(self, kind, payload):
            order.append(kind)
            if kind == "timer":
                return
            super().handle(kind, payload)

    engine().run_closed_loop([book(0)], Tie())
    assert order == ["strategy_observation", "timer", "decision"]


def test_adverse_ticks_never_exceed_the_limit() -> None:
    capped = Agent(orders=[(0, intent(price="0.50"))])
    result = engine(adverse_price_ticks=1).run_closed_loop([book(0), book(MS)], capped)
    assert not fills(result)
    room = Agent(orders=[(0, intent(oid="b", price="0.51"))])
    result = engine(adverse_price_ticks=1).run_closed_loop([book(0), book(MS)], room)
    (fill,) = fills(result)
    assert fill["price"] == Decimal("0.51")


def test_closed_loop_only_scenario_fields_rejected_for_scripted_adapter() -> None:
    with pytest.raises(ValueError):
        replace(Scenario(), feedback_delay_ms=10).validate()


def test_proxy_labels_settle_held_inventory() -> None:
    agent = Agent(orders=[(0, intent(q="4", price="0.50"))])
    labels = [{"instrument_id": IID, "status": "PROXY_OUTCOME", "payout": "1", "available_at": 0}]
    result = engine().run_closed_loop(
        [book(0), book(MS)], agent, labels, accepted_label_statuses=("PROXY_OUTCOME",)
    )
    assert result["payout_receivables"][IID] == Decimal("4")
    (receivable,) = [e for e in result["ledger"] if e["event"] == "payout_receivable"]
    assert receivable["label_status"] == "PROXY_OUTCOME"
    # Not accepted unless explicitly requested.
    plain = engine().run_closed_loop([book(0), book(MS)], Agent(), labels)
    assert plain["payout_receivables"] == {}
