"""Tests for Z-Gap numerical sanity and sigma seed corrections."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tyrex_pm.quant.model_sanity import (
    ModelSanityConfig,
    evaluate_model_numeric_sanity,
    recompute_z,
    simple_realized_sigma_per_sqrt_second,
    sigma_ratio_warning,
)
from tyrex_pm.quant.binary_fair_value import FairValueSnapshot, MODEL_STATUS_READY, compute_fair_value, FairValueInput
from tyrex_pm.quant.volatility import EwmaVolatilityEstimator, SigmaConfig, VolatilitySnapshot
from tyrex_pm.runtime.z_gap_sigma_warmup import _fetch_binance_agg_trades_sync
from tyrex_pm.state.signal_state_store import SignalStateStore
from tyrex_pm.strategies.z_gap.entry_eval import REASON_MODEL_NUMERIC_ANOMALY, evaluate_z_gap_entry

UTC = timezone.utc


def _ts(sec: float) -> datetime:
    return datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=sec)


def test_aggtrade_ms_timestamps_normalized_once() -> None:
    rows = _fetch_binance_agg_trades_sync(symbol="BTCUSDT", limit=5)
    assert rows
    for price, ts in rows:
        assert price > 0
        assert ts.tzinfo is not None
        assert ts.year >= 2020


def test_kline_ms_timestamps_remain_seconds_datetime() -> None:
    from tyrex_pm.runtime.z_gap_sigma_warmup import _fetch_binance_klines_sync

    rows = _fetch_binance_klines_sync(symbol="BTCUSDT", limit=5, interval="1s")
    assert rows
    ts0 = rows[0][1].timestamp()
    assert ts0 > 1_600_000_000


def test_seeded_and_live_use_same_datetime_unit() -> None:
    est = EwmaVolatilityEstimator(SigmaConfig(min_samples_s=2, sample_interval_s=1, jump_guard=False))
    est.seed_observations([(Decimal("100"), _ts(0)), (Decimal("100.01"), _ts(1))])
    snap = est.update(Decimal("100.02"), _ts(2))
    assert snap.sample_count >= 2


def test_synthetic_log_returns() -> None:
    est = EwmaVolatilityEstimator(SigmaConfig(min_samples_s=1, sample_interval_s=1, jump_guard=False))
    est.update(Decimal("100"), _ts(0))
    snap = est.update(Decimal("101"), _ts(1))
    expected_r = math.log(1.01)
    assert snap.sigma is not None
    assert abs(snap.sigma - expected_r) < 1e-9


def test_synthetic_sigma_per_sqrt_second() -> None:
    est = EwmaVolatilityEstimator(SigmaConfig(min_samples_s=1, sample_interval_s=1, jump_guard=False))
    est.update(Decimal("100"), _ts(0))
    snap = est.update(Decimal("101"), _ts(1))
    assert snap.sigma == pytest.approx(math.log(1.01))


def test_hand_calculated_z_matches_formula() -> None:
    s = Decimal("64169.995")
    k = Decimal("64111.16126412192")
    sigma = 4e-5
    tau = 297.0
    vol = VolatilitySnapshot(
        sigma=sigma,
        sigma_units="per_sqrt_second",
        ready=True,
        sample_count=20,
        effective_samples_s=20.0,
        last_update_ts=_ts(0),
        jump_guard_tripped=False,
        reject_reason=None,
    )
    fair = compute_fair_value(
        FairValueInput(S=s, K=k, sigma=sigma, tau_s=tau, tau_floor_s=1.0, snapshot_ts=_ts(0)),
        vol=vol,
    )
    manual = recompute_z(s=s, k=k, sigma=sigma, tau_s=tau, tau_floor_s=1.0)
    assert fair.z is not None and manual is not None
    assert fair.z == pytest.approx(manual)
    assert abs(fair.z) < 8


def test_simple_sigma_comparable_to_ewma_on_aggtrades() -> None:
    from tyrex_pm.runtime.z_gap_sigma_warmup import (
        _filter_duplicate_prices,
        _resample_observations_to_interval,
    )

    rows = _fetch_binance_agg_trades_sync(symbol="BTCUSDT", limit=200)
    resampled = _filter_duplicate_prices(
        _resample_observations_to_interval(rows, sample_interval_s=1.0)
    )
    simple = simple_realized_sigma_per_sqrt_second(resampled, sample_interval_s=1.0)
    est = EwmaVolatilityEstimator(SigmaConfig(min_samples_s=5, sample_interval_s=1, jump_guard=False))
    est.seed_observations(resampled)
    ewma = est.snapshot().sigma
    assert simple is not None and ewma is not None
    ratio = ewma / simple
    assert 0.01 <= ratio <= 100.0


def test_variance_not_used_as_sigma_directly() -> None:
    est = EwmaVolatilityEstimator(SigmaConfig(min_samples_s=1, sample_interval_s=1, jump_guard=False))
    est.update(Decimal("100"), _ts(0))
    snap = est.update(Decimal("101"), _ts(1))
    r = math.log(1.01)
    assert snap.sigma == pytest.approx(math.sqrt(r * r / 1.0))
    assert snap.sigma != pytest.approx(r * r)


def test_extreme_z_blocks_experimental_live() -> None:
    from test_z_gap_entry_eval import ENTRY, FEE_MODEL, _books, _edge, _fair, _signal, _vol
    from tyrex_pm.runtime.config import Z_GAP_ENTRY_MODE_ENFORCE
    from tyrex_pm.runtime.time_authority import SYNC_STATUS_SYNCED, TimeAuthority

    fair = _fair(z=25.0)
    ev = evaluate_z_gap_entry(
        signal=_signal(basis_bps=Decimal("1")),
        fair=fair,
        edge=_edge(fair),
        vol=_vol(),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
        entry_mode=Z_GAP_ENTRY_MODE_ENFORCE,
        time_authority=TimeAuthority(sync_status=SYNC_STATUS_SYNCED, samples_requested=5, samples_kept=3, uncertainty_ms=50.0),
        numeric_anomaly_block=True,
    )
    assert ev.reason_code == REASON_MODEL_NUMERIC_ANOMALY


def test_observe_anomaly_warn_does_not_require_block() -> None:
    fair = FairValueSnapshot(
        S=Decimal("100"),
        K=Decimal("99"),
        tau_s=120.0,
        sigma=1e-8,
        sigma_units="per_sqrt_second",
        z=50.0,
        p_up=1.0,
        p_down=0.0,
        model_status=MODEL_STATUS_READY,
        reject_reason=None,
        snapshot_ts=_ts(0),
    )
    sanity = evaluate_model_numeric_sanity(
        fair,
        cfg=ModelSanityConfig(warn_abs_z=8, block_abs_z=20),
        block_entries=False,
    )
    assert sanity.warn
    assert not sanity.block_entry


def test_strategy_thresholds_unchanged_in_yaml() -> None:
    from pathlib import Path
    import yaml

    raw = yaml.safe_load((Path("config/strategies/z_gap.yaml")).read_text(encoding="utf-8"))
    entry = raw["z_gap"]["entry"]
    assert entry["basis_max_bps"] == "3"
    assert entry["z_band"] == ["0.8", "2.2"]


def test_sigma_ratio_warning_flags_implausible_ratio() -> None:
    assert sigma_ratio_warning(ewma_sigma=1e-8, simple_sigma=1e-4, cfg=ModelSanityConfig()) is not None


def test_zero_return_freeze_prevents_sigma_decay() -> None:
    est = EwmaVolatilityEstimator(SigmaConfig(min_samples_s=1, sample_interval_s=1, jump_guard=False))
    est.update(Decimal("100"), _ts(0))
    snap = est.update(Decimal("101"), _ts(1))
    base_sigma = snap.sigma
    assert base_sigma is not None and base_sigma > 0
    for i in range(2, 32):
        snap = est.update(Decimal("101"), _ts(float(i)))
    assert snap.sigma == pytest.approx(base_sigma)


def test_resample_observations_one_per_second_bucket() -> None:
    from tyrex_pm.runtime.z_gap_sigma_warmup import _resample_observations_to_interval

    obs = [
        (Decimal("100"), _ts(0.1)),
        (Decimal("100.5"), _ts(0.9)),
        (Decimal("101"), _ts(1.2)),
        (Decimal("101"), _ts(1.8)),
    ]
    resampled = _resample_observations_to_interval(obs, sample_interval_s=1.0)
    assert len(resampled) == 2
    assert resampled[0][0] == Decimal("100.5")
    assert resampled[1][0] == Decimal("101")


def test_volatility_observation_prefers_aggtrade() -> None:
    store = SignalStateStore()
    now = _ts(10)
    store.update_binance(
        Decimal("64000.10"),
        source_ts=_ts(9),
        recv_ts=_ts(9),
        stream="bookTicker",
    )
    store.update_binance(
        Decimal("64000.55"),
        source_ts=_ts(10),
        recv_ts=_ts(10),
        stream="aggTrade",
    )
    obs = store.volatility_price_observation(now)
    assert obs is not None
    assert obs[0] == Decimal("64000.55")
