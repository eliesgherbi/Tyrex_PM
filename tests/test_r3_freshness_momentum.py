"""Freshness and momentum indicator tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.snapshots import ReferencePriceSnapshot
from tyrex_pm.indicators.momentum import MomentumConfig, ShortHorizonMomentum
from tyrex_pm.market_data.freshness import (
    FreshnessReason,
    TimestampBasis,
    assess_freshness,
)


def test_uninitialized_vs_stale() -> None:
    clock = FakeClock(_wall=datetime(2026, 7, 16, 12, 0, 10, tzinfo=timezone.utc))
    u = assess_freshness(
        clock=clock,
        ts_event=None,
        ts_received=None,
        initialized=False,
        threshold_ms=1000,
        future_tolerance_ms=500,
        basis=TimestampBasis.EVENT_TIME,
    )
    assert u.reason_code is FreshnessReason.UNINITIALIZED
    assert u.is_fresh is False

    clock.set_utc(datetime(2026, 7, 16, 12, 0, 10, tzinfo=timezone.utc))
    s = assess_freshness(
        clock=clock,
        ts_event=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc),
        ts_received=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc),
        initialized=True,
        threshold_ms=1000,
        future_tolerance_ms=500,
        basis=TimestampBasis.EVENT_TIME,
    )
    assert s.reason_code is FreshnessReason.STALE


def test_future_timestamp_rejected() -> None:
    clock = FakeClock(_wall=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc))
    f = assess_freshness(
        clock=clock,
        ts_event=datetime(2026, 7, 16, 12, 0, 5, tzinfo=timezone.utc),
        ts_received=datetime(2026, 7, 16, 12, 0, 5, tzinfo=timezone.utc),
        initialized=True,
        threshold_ms=10_000,
        future_tolerance_ms=500,
        basis=TimestampBasis.EVENT_TIME,
    )
    assert f.reason_code is FreshnessReason.FUTURE_TIMESTAMP
    assert f.is_fresh is False


def test_fresh_ok() -> None:
    clock = FakeClock(_wall=datetime(2026, 7, 16, 12, 0, 1, tzinfo=timezone.utc))
    f = assess_freshness(
        clock=clock,
        ts_event=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc),
        ts_received=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc),
        initialized=True,
        threshold_ms=5000,
        future_tolerance_ms=500,
        basis=TimestampBasis.EVENT_TIME,
    )
    assert f.is_fresh is True
    assert f.reason_code is FreshnessReason.FRESH


def test_momentum_insufficient_and_ready() -> None:
    ind = ShortHorizonMomentum(MomentumConfig(lookback=timedelta(seconds=5)))
    t0 = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    r0 = ind.update(ReferencePriceSnapshot(symbol="BTCUSDT", price=Decimal("100"), ts_event=t0))
    assert r0.value["ready"] is False
    t1 = t0 + timedelta(seconds=6)
    r1 = ind.update(ReferencePriceSnapshot(symbol="BTCUSDT", price=Decimal("101"), ts_event=t1))
    assert r1.value["ready"] is True
    assert r1.value["momentum"] == Decimal("101") / Decimal("100") - 1


def test_momentum_out_of_order_ignored() -> None:
    ind = ShortHorizonMomentum(MomentumConfig(lookback=timedelta(seconds=5)))
    t0 = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    ind.update(ReferencePriceSnapshot(symbol="BTCUSDT", price=Decimal("100"), ts_event=t0))
    ind.update(
        ReferencePriceSnapshot(
            symbol="BTCUSDT",
            price=Decimal("99"),
            ts_event=t0 - timedelta(seconds=1),
        )
    )
    t1 = t0 + timedelta(seconds=6)
    r = ind.update(ReferencePriceSnapshot(symbol="BTCUSDT", price=Decimal("102"), ts_event=t1))
    assert r.value["ready"] is True
    assert r.value["momentum"] == Decimal("102") / Decimal("100") - 1


def test_stale_as_clock_advances() -> None:
    clock = FakeClock(_wall=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc))
    ts = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    assert assess_freshness(
        clock=clock,
        ts_event=ts,
        ts_received=ts,
        initialized=True,
        threshold_ms=1000,
        future_tolerance_ms=0,
        basis=TimestampBasis.EVENT_TIME,
    ).is_fresh
    clock.advance(wall=timedelta(seconds=2))
    assert (
        assess_freshness(
            clock=clock,
            ts_event=ts,
            ts_received=ts,
            initialized=True,
            threshold_ms=1000,
            future_tolerance_ms=0,
            basis=TimestampBasis.EVENT_TIME,
        ).reason_code
        is FreshnessReason.STALE
    )
