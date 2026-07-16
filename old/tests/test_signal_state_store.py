"""Tests for SignalStateStore (A0.2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tyrex_pm.state.signal_state_store import (
    BASIS_FRESH,
    BASIS_MISSING,
    BASIS_UNTRUSTED,
    FRESHNESS_FRESH,
    FRESHNESS_LATE,
    FRESHNESS_MISSING,
    FRESHNESS_OBSERVED,
    FRESHNESS_STALE,
    SignalStateStore,
    compute_basis_bps,
)

UTC = timezone.utc


def _ts(offset_ms: float = 0) -> datetime:
    base = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)
    return base + timedelta(milliseconds=offset_ms)


def test_binance_update_stores_price_and_timestamps() -> None:
    store = SignalStateStore()
    recv = _ts()
    store.update_binance(
        Decimal("100100"),
        source_ts=_ts(-50),
        recv_ts=recv,
        stream="bookTicker",
    )
    snap = store.snapshot(now=recv)
    assert snap.binance_price == Decimal("100100")
    assert snap.binance_recv_ts == recv
    assert snap.binance_source_ts == _ts(-50)


def test_chainlink_update_stores_price_and_timestamps() -> None:
    store = SignalStateStore()
    recv = _ts()
    store.update_chainlink(Decimal("100000"), source_ts=_ts(-20), recv_ts=recv)
    snap = store.snapshot(now=recv)
    assert snap.chainlink_price == Decimal("100000")
    assert snap.chainlink_recv_ts == recv


def test_ptb_update_stores_k_and_lag() -> None:
    store = SignalStateStore()
    observed = _ts()
    store.update_price_to_beat(
        Decimal("99990"),
        status=FRESHNESS_OBSERVED,
        observed_ts=observed,
        lag_ms=120.0,
    )
    snap = store.snapshot(now=observed)
    assert snap.price_to_beat == Decimal("99990")
    assert snap.ptb_status == FRESHNESS_OBSERVED
    assert snap.ptb_lag_ms == 120.0


def test_snapshot_computes_ages_from_source_ts() -> None:
    """Binance age uses venue source_ts when present (aligned with corrected-now freshness)."""
    store = SignalStateStore()
    now = _ts(5000)
    store.update_binance(Decimal("1"), source_ts=_ts(0), recv_ts=_ts(1000), stream="bookTicker")
    snap = store.snapshot(now=now)
    assert snap.binance_age_ms == pytest.approx(5000.0)
    assert snap.binance_freshness == FRESHNESS_STALE


def test_snapshot_age_falls_back_to_recv_when_source_missing() -> None:
    store = SignalStateStore()
    now = _ts(5000)
    store.update_binance(Decimal("1"), source_ts=None, recv_ts=_ts(1000), stream="bookTicker")
    snap = store.snapshot(now=now)
    assert snap.binance_age_ms == pytest.approx(4000.0)
    assert snap.binance_freshness == FRESHNESS_STALE


def test_chainlink_snapshot_computes_age_from_source_ts() -> None:
    store = SignalStateStore()
    now = _ts(3000)
    store.update_chainlink(Decimal("100"), source_ts=_ts(500), recv_ts=_ts(2000))
    snap = store.snapshot(now=now)
    assert snap.chainlink_age_ms == pytest.approx(2500.0)
    assert snap.chainlink_freshness == FRESHNESS_FRESH


def test_fresh_feeds_ready_for_observe() -> None:
    store = SignalStateStore(binance_max_age_ms=2000, chainlink_max_age_ms=3000)
    now = _ts(1000)
    store.update_binance(Decimal("100"), source_ts=_ts(0), recv_ts=_ts(500), stream="bookTicker")
    store.update_chainlink(Decimal("99"), source_ts=_ts(0), recv_ts=_ts(600))
    ready, reason = store.is_ready_for_observe(now=now)
    assert ready is True
    assert reason is None


def test_missing_binance_not_ready() -> None:
    store = SignalStateStore()
    now = _ts()
    store.update_chainlink(Decimal("99"), source_ts=now, recv_ts=now)
    ready, reason = store.is_ready_for_observe(now=now)
    assert ready is False
    assert reason == "binance_missing"


def test_missing_chainlink_not_ready() -> None:
    store = SignalStateStore()
    now = _ts()
    store.update_binance(Decimal("100"), source_ts=now, recv_ts=now, stream="bookTicker")
    ready, reason = store.is_ready_for_observe(now=now)
    assert ready is False
    assert reason == "chainlink_missing"


def test_stale_chainlink_marks_basis_untrusted() -> None:
    store = SignalStateStore(chainlink_max_age_ms=1000)
    now = _ts(5000)
    store.update_binance(Decimal("101000"), source_ts=_ts(0), recv_ts=_ts(4500), stream="bookTicker")
    store.update_chainlink(Decimal("100000"), source_ts=_ts(0), recv_ts=_ts(1000))
    snap = store.snapshot(now=now)
    assert snap.chainlink_freshness == FRESHNESS_STALE
    assert snap.basis_status == BASIS_UNTRUSTED
    assert snap.basis_bps is not None


def test_fresh_chainlink_computes_basis() -> None:
    store = SignalStateStore()
    now = _ts(500)
    store.update_binance(Decimal("100300"), source_ts=now, recv_ts=now, stream="bookTicker")
    store.update_chainlink(Decimal("100000"), source_ts=now, recv_ts=now)
    snap = store.snapshot(now=now)
    assert snap.basis_status == BASIS_FRESH
    assert snap.basis_bps == Decimal("30")


def test_basis_bps_decimal_math() -> None:
    assert compute_basis_bps(Decimal("100300"), Decimal("100000")) == Decimal("30")


def test_ptb_late_status_when_lag_high() -> None:
    store = SignalStateStore(ptb_late_threshold_ms=1000)
    store.update_price_to_beat(
        Decimal("100"),
        status=FRESHNESS_OBSERVED,
        observed_ts=_ts(),
        lag_ms=2500.0,
    )
    snap = store.snapshot(now=_ts())
    assert snap.ptb_status == FRESHNESS_LATE


def test_ptb_pending_and_missing_statuses() -> None:
    store = SignalStateStore()
    snap = store.snapshot(now=_ts())
    assert snap.ptb_status == "pending"
    store.update_price_to_beat(None, status="missing", observed_ts=None, lag_ms=None)
    snap = store.snapshot(now=_ts())
    assert snap.ptb_status == "missing"


def test_snapshot_is_immutable() -> None:
    store = SignalStateStore()
    store.update_binance(Decimal("1"), source_ts=_ts(), recv_ts=_ts(), stream="bookTicker")
    snap = store.snapshot(now=_ts())
    with pytest.raises(Exception):
        snap.binance_price = Decimal("2")  # type: ignore[misc]


def test_missing_basis_when_prices_incomplete() -> None:
    store = SignalStateStore()
    snap = store.snapshot(now=_ts())
    assert snap.basis_status == BASIS_MISSING
