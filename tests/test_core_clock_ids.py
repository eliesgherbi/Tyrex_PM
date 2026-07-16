"""Clock and identifier contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tyrex_pm.core.clock import FakeClock, NaiveDateTimeError, require_utc
from tyrex_pm.core.ids import (
    CorrelationId,
    EventId,
    InstrumentId,
    MarketId,
    RunId,
    StrategyId,
    TokenId,
    new_correlation_id,
    new_event_id,
)


def test_require_utc_rejects_naive() -> None:
    with pytest.raises(NaiveDateTimeError):
        require_utc(datetime(2026, 7, 16, 12, 0, 0))


def test_require_utc_normalizes_offset() -> None:
    aware = datetime(2026, 7, 16, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert require_utc(aware) == datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def test_fake_clock_deterministic() -> None:
    start = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    clock = FakeClock(_wall=start, _mono_ns=100)
    assert clock.now_utc() == start
    assert clock.monotonic_ns() == 100
    clock.advance(wall=timedelta(seconds=5), mono_ns=7)
    assert clock.now_utc() == start + timedelta(seconds=5)
    assert clock.monotonic_ns() == 107


def test_ids_reject_empty_and_can_be_injected() -> None:
    with pytest.raises(ValueError):
        EventId("  ")
    eid = EventId("evt-1")
    cid = CorrelationId("corr-1")
    assert eid.value == "evt-1"
    assert cid.value == "corr-1"
    assert isinstance(new_event_id(), EventId)
    assert isinstance(new_correlation_id(), CorrelationId)
    assert InstrumentId("tok").value == TokenId("tok").value
    assert MarketId("m1").value == "m1"
    assert StrategyId("s1").value == "s1"
    assert RunId("r1").value == "r1"
