"""Event immutability, causality, and snapshot validation."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tyrex_pm.core.events import BookUpdated, EventSource, ReferencePriceUpdated, TimerElapsed
from tyrex_pm.core.ids import CorrelationId, EventId, InstrumentId
from tyrex_pm.core.numerics import NumericError
from tyrex_pm.core.snapshots import BookLevel, BookSnapshot, ReferencePriceSnapshot


UTC = timezone.utc


def _ts(h: int = 12) -> datetime:
    return datetime(2026, 7, 16, h, 0, 0, tzinfo=UTC)


def test_book_snapshot_orders_and_rejects_cross() -> None:
    snap = BookSnapshot.from_levels(
        instrument_id=InstrumentId("t1"),
        ts_event=_ts(),
        bids=[("0.4", "10"), ("0.5", "5"), ("0.5", "99")],
        asks=[("0.6", "3"), ("0.55", "2")],
    )
    assert [level.price for level in snap.bids] == [Decimal("0.5"), Decimal("0.4")]
    assert [level.price for level in snap.asks] == [Decimal("0.55"), Decimal("0.6")]
    assert snap.best_bid is not None and snap.best_bid.quantity == Decimal("5")

    with pytest.raises(ValueError, match="crossed"):
        BookSnapshot.from_levels(
            instrument_id=InstrumentId("t1"),
            ts_event=_ts(),
            bids=[("0.6", "1")],
            asks=[("0.5", "1")],
        )


def test_rejects_float_and_out_of_range_price() -> None:
    with pytest.raises(NumericError):
        BookLevel(price=0.5, quantity=1)  # type: ignore[arg-type]
    with pytest.raises(NumericError):
        BookLevel(price=Decimal("1.5"), quantity=Decimal("1"))


def test_events_immutable_and_causality() -> None:
    book = BookSnapshot.from_levels(
        instrument_id=InstrumentId("t1"),
        ts_event=_ts(),
        bids=[("0.4", "1")],
        asks=[("0.6", "1")],
    )
    root = BookUpdated(
        event_id=EventId("e1"),
        correlation_id=CorrelationId("c1"),
        ts_event=_ts(),
        ts_received=_ts(),
        source=EventSource.POLYMARKET_CLOB,
        book=book,
    )
    assert root.causation_id is None
    derived = BookUpdated(
        event_id=EventId("e2"),
        correlation_id=root.correlation_id,
        causation_id=root.event_id,
        ts_event=_ts(13),
        ts_received=_ts(13),
        source=EventSource.SYSTEM,
        book=book,
    )
    assert derived.correlation_id == root.correlation_id
    assert derived.causation_id == root.event_id
    with pytest.raises(Exception):
        root.event_id = EventId("x")  # type: ignore[misc]


def test_reference_and_timer_events() -> None:
    ref = ReferencePriceSnapshot(
        symbol="BTCUSDT",
        price=Decimal("100000"),
        ts_event=_ts(),
    )
    evt = ReferencePriceUpdated(
        event_id=EventId("r1"),
        correlation_id=CorrelationId("c1"),
        ts_event=_ts(),
        ts_received=_ts(),
        source=EventSource.BINANCE,
        reference=ref,
    )
    assert evt.reference.symbol == "BTCUSDT"
    timer = TimerElapsed(
        event_id=EventId("t1"),
        correlation_id=CorrelationId("c1"),
        causation_id=evt.event_id,
        ts_event=_ts(),
        ts_received=_ts(),
        source=EventSource.TIMER,
        timer_name="heartbeat",
    )
    assert timer.timer_name == "heartbeat"
