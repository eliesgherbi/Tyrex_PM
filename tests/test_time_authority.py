"""Tests for application-level TimeAuthority (A0.5 timing fix)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from tyrex_pm.market_data.book_read import LegBookQuote, PairBookSnapshot, QUALITY_OK
from tyrex_pm.quant.binary_fair_value import FairValueSnapshot, MODEL_STATUS_READY
from tyrex_pm.quant.edge import EDGE_STATUS_READY, EdgeSnapshot
from tyrex_pm.quant.fees import parse_fee_model_from_raw
from tyrex_pm.quant.volatility import VolatilitySnapshot
from tyrex_pm.runtime.config import Z_GAP_ENTRY_MODE_ENFORCE, Z_GAP_ENTRY_MODE_OBSERVE_ONLY, _parse_z_gap_entry
from tyrex_pm.runtime.time_authority import (
    SyncSample,
    TimeAuthority,
    compute_offset_ms,
    reset_feeds_started_for_tests,
    sample_binance_offsets,
    sample_sntp_offsets,
    select_best_samples,
    sync_time_authority,
)
from tyrex_pm.state.signal_state_store import FRESHNESS_FRESH, FRESHNESS_OBSERVED, FRESHNESS_STALE, SignalSnapshot
from tyrex_pm.strategies.z_gap.entry_eval import (
    DECISION_SKIP,
    REASON_CLOCK_SYNC_FAILED,
    evaluate_z_gap_entry,
)

ENTRY = _parse_z_gap_entry({})
FEE_MODEL = parse_fee_model_from_raw({"fd": {"r": 0.07, "e": 1, "to": True}})
TS = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_feeds_flag() -> None:
    reset_feeds_started_for_tests()


def _signal() -> SignalSnapshot:
    return SignalSnapshot(
        binance_price=Decimal("100100"),
        binance_source_ts=TS,
        binance_recv_ts=TS,
        binance_age_ms=100.0,
        binance_freshness=FRESHNESS_FRESH,
        chainlink_price=Decimal("100000"),
        chainlink_source_ts=TS,
        chainlink_recv_ts=TS,
        chainlink_age_ms=100.0,
        chainlink_freshness=FRESHNESS_FRESH,
        price_to_beat=Decimal("100000"),
        ptb_status=FRESHNESS_OBSERVED,
        ptb_observed_ts=TS,
        ptb_lag_ms=500.0,
        basis_bps=Decimal("1"),
        basis_status="fresh",
        feed_reject_reason=None,
        snapshot_ts=TS,
    )


def _fair() -> FairValueSnapshot:
    return FairValueSnapshot(
        S=Decimal("100100"),
        K=Decimal("100000"),
        tau_s=120.0,
        sigma=0.00015,
        sigma_units="per_sqrt_second",
        z=1.0,
        p_up=0.6,
        p_down=0.4,
        model_status=MODEL_STATUS_READY,
        reject_reason=None,
        snapshot_ts=TS,
    )


def _vol() -> VolatilitySnapshot:
    return VolatilitySnapshot(
        sigma=0.00015,
        sigma_units="per_sqrt_second",
        ready=True,
        sample_count=30,
        effective_samples_s=30.0,
        last_update_ts=TS,
        jump_guard_tripped=False,
        reject_reason=None,
    )


def _books() -> PairBookSnapshot:
    return PairBookSnapshot(
        up=LegBookQuote("111", Decimal("0.60"), Decimal("0.61"), False, 100, Decimal("0.01"), QUALITY_OK),
        down=LegBookQuote("222", Decimal("0.38"), Decimal("0.39"), False, 100, Decimal("0.01"), QUALITY_OK),
    )


def _edge(fair: FairValueSnapshot) -> EdgeSnapshot:
    from tyrex_pm.quant.edge import compute_edge

    return compute_edge(
        fair,
        ask_up=Decimal("0.61"),
        ask_down=Decimal("0.39"),
        fee_model=FEE_MODEL,
    )


def test_offset_math_golden() -> None:
    t_before = 1000.0
    server_ms = 1_000_250.0
    t_after = 1000.2
    offset_ms, rtt_ms = compute_offset_ms(t_before=t_before, server_time_ms=server_ms, t_after=t_after)
    assert rtt_ms == pytest.approx(200.0)
    assert offset_ms == pytest.approx(150.0)


def test_select_best_3_of_7_by_rtt() -> None:
    samples = [SyncSample(rtt_ms=float(i * 50), offset_ms=10.0, source="sntp") for i in range(1, 8)]
    kept = select_best_samples(samples, keep=3)
    assert len(kept) == 3
    assert [s.rtt_ms for s in kept] == [50.0, 100.0, 150.0]


def test_binance_sampler_keeps_best_by_rtt_not_hard_discard() -> None:
    client = MagicMock()
    client.fetch_server_time_ms.return_value = 1_000_100.0
    with patch("tyrex_pm.runtime.time_authority.time.time") as mock_time:
        mock_time.side_effect = [1000.0, 1000.05, 1000.0, 1000.5] * 4
        kept = sample_binance_offsets(samples_total=4, samples_kept=2, client=client)
    assert len(kept) == 2
    assert kept[0].rtt_ms <= kept[1].rtt_ms


def test_sync_fails_if_sntp_samples_empty() -> None:
    with patch("tyrex_pm.runtime.time_authority.sample_sntp_offsets", return_value=[]):
        ta = sync_time_authority(require_feeds_not_started=False, max_attempts=1)
    assert ta.sync_status == "failed"
    assert ta.samples_kept == 0
    assert not ta.enforce_gate_pass


def test_sync_succeeds_with_mock_sntp() -> None:
    kept = [
        SyncSample(rtt_ms=18.0, offset_ms=12.0, source="sntp"),
        SyncSample(rtt_ms=20.0, offset_ms=11.0, source="sntp"),
        SyncSample(rtt_ms=22.0, offset_ms=13.0, source="sntp"),
    ]
    binance_kept = [
        SyncSample(rtt_ms=200.0, offset_ms=15.0, source="binance"),
        SyncSample(rtt_ms=210.0, offset_ms=14.0, source="binance"),
        SyncSample(rtt_ms=220.0, offset_ms=16.0, source="binance"),
    ]
    with patch("tyrex_pm.runtime.time_authority.sample_sntp_offsets", return_value=kept):
        with patch("tyrex_pm.runtime.time_authority.sample_binance_offsets", return_value=binance_kept):
            ta = sync_time_authority(require_feeds_not_started=False, max_attempts=1)
    assert ta.sync_status == "synced"
    assert ta.samples_kept == 3
    assert ta.uncertainty_ms == pytest.approx(11.0)
    assert ta.source == "sntp_time_cloudflare_com"
    assert ta.enforce_gate_pass


def test_sample_offset_asserts_if_feeds_started() -> None:
    from tyrex_pm.runtime.time_authority import mark_feeds_started, sample_offset

    mark_feeds_started(caller="test")
    with pytest.raises(RuntimeError, match="before feeds start"):
        sample_offset()


def test_corrected_now_uses_monotonic_anchor() -> None:
    ta = TimeAuthority(
        sync_status="synced",
        offset_ms=100.0,
        uncertainty_ms=50.0,
        median_offset_ms=100.0,
        samples_requested=5,
        samples_kept=5,
        max_rtt_ms=100.0,
        _epoch_at_sync=2000.0,
        _mono_at_sync=10.0,
    )
    with patch("tyrex_pm.runtime.time_authority.time.monotonic", return_value=12.5):
        assert ta.corrected_epoch() == pytest.approx(2002.5)


def test_wall_clock_step_does_not_jump_corrected_time() -> None:
    ta = TimeAuthority(
        sync_status="synced",
        offset_ms=0.0,
        uncertainty_ms=10.0,
        median_offset_ms=0.0,
        samples_requested=1,
        samples_kept=1,
        max_rtt_ms=20.0,
        _epoch_at_sync=1000.0,
        _mono_at_sync=50.0,
    )
    with patch("tyrex_pm.runtime.time_authority.time.monotonic", return_value=55.0):
        t1 = ta.corrected_epoch()
    with patch("tyrex_pm.runtime.time_authority.time.monotonic", return_value=55.0):
        with patch("tyrex_pm.runtime.time_authority.time.time", return_value=5000.0):
            t2 = ta.corrected_epoch()
    assert t1 == pytest.approx(t2)


def test_feed_freshness_uses_corrected_time() -> None:
    from tyrex_pm.state.signal_state_store import SignalStateStore

    store = SignalStateStore()
    source_ts = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
    store.update_binance(
        Decimal("100"),
        source_ts=source_ts,
        recv_ts=source_ts,
        stream="bookTicker",
    )
    ta = TimeAuthority(
        sync_status="synced",
        offset_ms=1500.0,
        uncertainty_ms=50.0,
        median_offset_ms=1500.0,
        samples_requested=1,
        samples_kept=1,
        max_rtt_ms=20.0,
        _epoch_at_sync=source_ts.timestamp() + 2.0,
        _mono_at_sync=100.0,
    )
    with patch("tyrex_pm.runtime.time_authority.time.monotonic", return_value=102.0):
        snap = store.snapshot(now=ta.corrected_now())
    assert snap.binance_age_ms == pytest.approx(4000.0, abs=50.0)
    assert snap.binance_freshness == FRESHNESS_STALE


def test_observe_mode_warns_not_skips_on_os_drift() -> None:
    ta = TimeAuthority(
        sync_status="synced",
        offset_ms=-1428.0,
        uncertainty_ms=80.0,
        median_offset_ms=-1428.0,
        samples_requested=5,
        samples_kept=5,
        max_rtt_ms=100.0,
        os_drift_ms=-1428.0,
        _epoch_at_sync=time.time(),
        _mono_at_sync=time.monotonic(),
    )
    evaln = evaluate_z_gap_entry(
        signal=_signal(),
        fair=_fair(),
        edge=_edge(_fair()),
        vol=_vol(),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
        time_authority=ta,
        entry_mode=Z_GAP_ENTRY_MODE_OBSERVE_ONLY,
    )
    assert evaln.gate_results.get("clock_sync") == "pass"
    assert evaln.reason_code != REASON_CLOCK_SYNC_FAILED


def test_enforce_mode_blocks_if_uncertainty_high() -> None:
    ta = TimeAuthority(
        sync_status="synced",
        offset_ms=10.0,
        uncertainty_ms=300.0,
        median_offset_ms=10.0,
        samples_requested=5,
        samples_kept=5,
        max_rtt_ms=600.0,
        _epoch_at_sync=time.time(),
        _mono_at_sync=time.monotonic(),
    )
    evaln = evaluate_z_gap_entry(
        signal=_signal(),
        fair=_fair(),
        edge=_edge(_fair()),
        vol=_vol(),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
        time_authority=ta,
        entry_mode=Z_GAP_ENTRY_MODE_ENFORCE,
    )
    assert evaln.decision_status == DECISION_SKIP
    assert evaln.reason_code == REASON_CLOCK_SYNC_FAILED
