"""Regression tests for PriceToBeatTracker atomicity and file derivation (A0.5)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tyrex_pm.ingestion.price_to_beat_tracker import (
    PTB_STATUS_LATE,
    PTB_STATUS_MISSING,
    PTB_STATUS_OBSERVED,
    PTB_STATUS_OBSERVED_FROM_LOG,
    PTB_STATUS_PENDING,
    PriceToBeatTracker,
    derive_ptb_from_chainlink_log,
    derive_ptb_from_chainlink_log_ex,
)
from tyrex_pm.state.signal_state_store import FRESHNESS_OBSERVED, SignalStateStore


def _write_tick(path: Path, *, source_ts: datetime, price: str) -> None:
    row = {
        "source_ts": source_ts.isoformat(),
        "recv_ts": source_ts.isoformat(),
        "price": price,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def test_never_emit_observed_with_null_k(tmp_path: Path) -> None:
    tracker = PriceToBeatTracker(max_lag_ms=5000.0)
    tracker.register_market(
        market_id="btc_5m_test",
        event_start_ts=1783634400.0,
        event_end_ts=1783634700.0,
    )
    state = tracker._markets["btc_5m_test"]
    state.ptb_status = PTB_STATUS_OBSERVED
    state.price_to_beat = None
    events = tracker._maybe_emit(state)
    assert events == []


def test_derive_from_log_usable_k(tmp_path: Path) -> None:
    log_path = tmp_path / "chainlink_ticks.jsonl"
    event_start = 1783634400.0
    tick_ts = datetime.fromtimestamp(event_start + 1.0, tz=timezone.utc)
    _write_tick(log_path, source_ts=tick_ts, price="63220.22")
    derived = derive_ptb_from_chainlink_log(
        event_start_ts=event_start,
        path=log_path,
        max_lag_ms=5000.0,
    )
    assert derived is not None
    assert derived.price == "63220.22"
    assert derived.status == PTB_STATUS_OBSERVED_FROM_LOG
    assert derived.boundary_lag_ms == pytest.approx(1000.0)


def test_derive_from_log_late_k(tmp_path: Path) -> None:
    log_path = tmp_path / "chainlink_ticks.jsonl"
    event_start = 1783634400.0
    tick_ts = datetime.fromtimestamp(event_start + 8.0, tz=timezone.utc)
    _write_tick(log_path, source_ts=tick_ts, price="63220.22")
    derived = derive_ptb_from_chainlink_log(
        event_start_ts=event_start,
        path=log_path,
        max_lag_ms=5000.0,
    )
    assert derived is not None
    assert derived.status == PTB_STATUS_LATE
    assert derived.price == "63220.22"
    assert derived.boundary_lag_ms == pytest.approx(8000.0)


def test_derive_from_log_no_matching_tick_returns_none(tmp_path: Path) -> None:
    log_path = tmp_path / "chainlink_ticks.jsonl"
    event_start = 1783634400.0
    tick_ts = datetime.fromtimestamp(event_start - 10.0, tz=timezone.utc)
    _write_tick(log_path, source_ts=tick_ts, price="63200.00")
    derived = derive_ptb_from_chainlink_log(
        event_start_ts=event_start,
        path=log_path,
        max_lag_ms=5000.0,
    )
    assert derived is None


def test_register_no_log_tick_stays_pending(tmp_path: Path) -> None:
    log_path = tmp_path / "chainlink_ticks.jsonl"
    event_start = 1783634400.0
    tracker = PriceToBeatTracker(max_lag_ms=5000.0, chainlink_log_path=log_path)
    derived = tracker.register_market(
        market_id="m_missing",
        event_start_ts=event_start,
        event_end_ts=event_start + 300,
    )
    state = tracker._markets["m_missing"]
    assert derived is None
    assert state.ptb_status == PTB_STATUS_PENDING
    assert state.price_to_beat is None
    events = tracker._maybe_emit(state)
    assert events == []


def test_late_k_not_calibration_usable() -> None:
    from tyrex_pm.strategies.z_gap.calibration_samples import is_calibration_usable_ptb
    from tyrex_pm.state.signal_state_store import FRESHNESS_LATE, SignalSnapshot
    from decimal import Decimal

    signal = SignalSnapshot(
        binance_price=Decimal("100"),
        binance_source_ts=None,
        binance_recv_ts=None,
        binance_age_ms=None,
        binance_freshness="fresh",
        chainlink_price=Decimal("100"),
        chainlink_source_ts=None,
        chainlink_recv_ts=None,
        chainlink_age_ms=None,
        chainlink_freshness="fresh",
        price_to_beat=Decimal("63220"),
        ptb_status=FRESHNESS_LATE,
        ptb_observed_ts=None,
        ptb_lag_ms=8000.0,
        basis_bps=None,
        basis_status="missing",
        feed_reject_reason=None,
        snapshot_ts=datetime.now(timezone.utc),
    )
    assert is_calibration_usable_ptb(signal) is False


def test_observed_from_log_is_calibration_usable() -> None:
    from tyrex_pm.strategies.z_gap.calibration_samples import is_calibration_usable_ptb
    from tyrex_pm.state.signal_state_store import FRESHNESS_OBSERVED_FROM_LOG, SignalSnapshot
    from decimal import Decimal

    signal = SignalSnapshot(
        binance_price=Decimal("100"),
        binance_source_ts=None,
        binance_recv_ts=None,
        binance_age_ms=None,
        binance_freshness="fresh",
        chainlink_price=Decimal("100"),
        chainlink_source_ts=None,
        chainlink_recv_ts=None,
        chainlink_age_ms=None,
        chainlink_freshness="fresh",
        price_to_beat=Decimal("63220"),
        ptb_status=FRESHNESS_OBSERVED_FROM_LOG,
        ptb_observed_ts=None,
        ptb_lag_ms=1000.0,
        basis_bps=None,
        basis_status="missing",
        feed_reject_reason=None,
        snapshot_ts=datetime.now(timezone.utc),
    )
    assert is_calibration_usable_ptb(signal) is True


def test_signal_store_never_observed_without_price() -> None:
    store = SignalStateStore()
    store.update_price_to_beat(None, status=FRESHNESS_OBSERVED)
    snap = store.snapshot()
    assert snap.ptb_status != FRESHNESS_OBSERVED
    assert snap.price_to_beat is None


def test_register_applies_log_derivation(tmp_path: Path) -> None:
    log_path = tmp_path / "chainlink_ticks.jsonl"
    event_start = 1783634400.0
    tick_ts = datetime.fromtimestamp(event_start, tz=timezone.utc)
    _write_tick(log_path, source_ts=tick_ts, price="63220.22")
    tracker = PriceToBeatTracker(max_lag_ms=5000.0, chainlink_log_path=log_path)
    derived = tracker.register_market(
        market_id="m1",
        event_start_ts=event_start,
        event_end_ts=event_start + 300,
    )
    state = tracker._markets["m1"]
    assert derived is not None
    assert state.ptb_status == PTB_STATUS_OBSERVED_FROM_LOG
    assert state.price_to_beat == "63220.22"
    events = tracker._maybe_emit(state)
    assert len(events) == 1
    payload = events[0].payload or {}
    assert payload.get("price_to_beat") == "63220.22"
    assert payload.get("status") == PTB_STATUS_OBSERVED_FROM_LOG


def test_derive_from_log_skips_blank_and_malformed_rows(tmp_path: Path) -> None:
    log_path = tmp_path / "chainlink_ticks.jsonl"
    event_start = 1783634400.0
    first_ts = datetime.fromtimestamp(event_start + 1.0, tz=timezone.utc)
    second_ts = datetime.fromtimestamp(event_start + 2.0, tz=timezone.utc)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "source_ts": first_ts.isoformat(),
                    "recv_ts": first_ts.isoformat(),
                    "price": "11111.11",
                }
            )
            + "\n"
        )
        fh.write("\n")
        fh.write("{not valid json\n")
        fh.write(
            json.dumps(
                {
                    "source_ts": second_ts.isoformat(),
                    "recv_ts": second_ts.isoformat(),
                    "price": "22222.22",
                }
            )
            + "\n"
        )
    derived, diagnostics = derive_ptb_from_chainlink_log_ex(
        event_start_ts=event_start,
        path=log_path,
        max_lag_ms=5000.0,
    )
    assert derived is not None
    assert derived.price == "11111.11"
    assert diagnostics.skipped_blank_lines == 1
    assert diagnostics.skipped_malformed_json == 1
    assert diagnostics.had_parse_issues is True
