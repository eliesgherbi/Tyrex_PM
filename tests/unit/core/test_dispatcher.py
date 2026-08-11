"""EventDispatcher semantics."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tyrex_pm.core.events import BookUpdated, Event, EventSource, TimerElapsed
from tyrex_pm.core.ids import CorrelationId, EventId, InstrumentId
from tyrex_pm.core.snapshots import BookSnapshot
from tyrex_pm.engine.dispatcher import DispatchError, EventDispatcher

UTC = timezone.utc


def _book_event(eid: str = "e1") -> BookUpdated:
    book = BookSnapshot.from_levels(
        instrument_id=InstrumentId("t1"),
        ts_event=datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC),
        bids=[("0.4", "1")],
        asks=[("0.6", "1")],
    )
    return BookUpdated(
        event_id=EventId(eid),
        correlation_id=CorrelationId("c1"),
        ts_event=datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC),
        ts_received=datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC),
        source=EventSource.POLYMARKET_CLOB,
        book=book,
    )


def _timer(eid: str, *, cause: EventId | None = None) -> TimerElapsed:
    return TimerElapsed(
        event_id=EventId(eid),
        correlation_id=CorrelationId("c1"),
        causation_id=cause,
        ts_event=datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC),
        ts_received=datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC),
        source=EventSource.TIMER,
        timer_name="t",
    )


def test_priority_then_subscription_order() -> None:
    d = EventDispatcher()
    order: list[str] = []

    def a(_: Event) -> None:
        order.append("a")

    def b(_: Event) -> None:
        order.append("b")

    def c(_: Event) -> None:
        order.append("c")

    d.subscribe(BookUpdated, a, priority=0)
    d.subscribe(BookUpdated, b, priority=10)
    d.subscribe(BookUpdated, c, priority=10)
    result = d.publish(_book_event())
    assert order == ["b", "c", "a"]
    assert result.delivered == 3
    assert result.handler_count == 3


def test_unsubscribe_and_no_subscribers() -> None:
    d = EventDispatcher()
    calls = []

    def h(_: Event) -> None:
        calls.append(1)

    sub = d.subscribe(BookUpdated, h)
    d.unsubscribe(sub)
    result = d.publish(_book_event())
    assert calls == []
    assert result.handler_count == 0
    assert result.delivered == 0


def test_duplicate_subscription_rejected() -> None:
    d = EventDispatcher()

    def h(_: Event) -> None:
        return None

    d.subscribe(BookUpdated, h)
    with pytest.raises(ValueError, match="duplicate"):
        d.subscribe(BookUpdated, h)


def test_exact_type_routing_not_base() -> None:
    d = EventDispatcher()
    seen: list[str] = []

    def on_event(_: Event) -> None:
        seen.append("base")

    def on_book(_: BookUpdated) -> None:
        seen.append("book")

    d.subscribe(Event, on_event)
    d.subscribe(BookUpdated, on_book)
    d.publish(_book_event())
    assert seen == ["book"]


def test_handler_failure_fail_fast() -> None:
    d = EventDispatcher()
    seen: list[str] = []

    def bad(_: Event) -> None:
        raise RuntimeError("boom")

    def after(_: Event) -> None:
        seen.append("after")

    d.subscribe(BookUpdated, bad, priority=10)
    d.subscribe(BookUpdated, after, priority=0)
    with pytest.raises(DispatchError) as exc:
        d.publish(_book_event())
    assert exc.value.event_id.value == "e1"
    assert seen == []


def test_reentrant_publish_queued_until_current_finishes() -> None:
    d = EventDispatcher()
    order: list[str] = []

    def parent(event: Event) -> None:
        order.append(f"parent:{event.event_id.value}")
        nested = d.publish(_timer("child", cause=event.event_id))
        assert nested.delivered == 0
        assert nested.queued_followups == 1
        order.append("parent:after-nested-call")

    def child(event: Event) -> None:
        order.append(f"child:{event.event_id.value}")

    d.subscribe(BookUpdated, parent)
    d.subscribe(TimerElapsed, child)
    result = d.publish(_book_event("parent"))
    assert order == [
        "parent:parent",
        "parent:after-nested-call",
        "child:child",
    ]
    assert result.queued_followups == 1


def test_subscribe_during_publish_applies_next_event() -> None:
    d = EventDispatcher()
    late_calls: list[str] = []
    subscribed = {"done": False}

    def late(_: Event) -> None:
        late_calls.append("late")

    def first(_: Event) -> None:
        if not subscribed["done"]:
            d.subscribe(BookUpdated, late)
            subscribed["done"] = True

    d.subscribe(BookUpdated, first)
    d.publish(_book_event("1"))
    assert late_calls == []
    d.publish(_book_event("2"))
    assert late_calls == ["late"]


def test_causality_propagation_example() -> None:
    """Root external event → derived timer retains correlation and causation."""
    d = EventDispatcher()
    derived: list[TimerElapsed] = []

    def on_book(event: BookUpdated) -> None:
        d.publish(_timer("derived", cause=event.event_id))

    def on_timer(event: TimerElapsed) -> None:
        derived.append(event)

    d.subscribe(BookUpdated, on_book)
    d.subscribe(TimerElapsed, on_timer)
    root = _book_event("root")
    d.publish(root)
    assert len(derived) == 1
    assert derived[0].correlation_id == root.correlation_id
    assert derived[0].causation_id == root.event_id
