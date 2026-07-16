"""Tests for EWMA volatility estimator (A0.3)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tyrex_pm.quant.volatility import (
    SIGMA_UNITS_PER_SQRT_SECOND,
    EwmaVolatilityEstimator,
    SigmaConfig,
)

UTC = timezone.utc


def _ts(sec: float) -> datetime:
    return datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=sec)


def _warm_estimator(
    *,
    min_samples_s: float = 5.0,
    sample_interval_s: float = 1.0,
    prices: list[float] | None = None,
) -> EwmaVolatilityEstimator:
    est = EwmaVolatilityEstimator(
        SigmaConfig(min_samples_s=min_samples_s, sample_interval_s=sample_interval_s, jump_guard=False)
    )
    series = prices or [100.0 + 0.01 * i for i in range(30)]
    snap = est.snapshot()
    for i, px in enumerate(series):
        snap = est.update(Decimal(str(px)), _ts(float(i)))
    return est


def test_sigma_units_explicit() -> None:
    est = EwmaVolatilityEstimator()
    assert est.sigma_units == SIGMA_UNITS_PER_SQRT_SECOND
    snap = est.snapshot()
    assert snap.sigma_units == SIGMA_UNITS_PER_SQRT_SECOND


def test_estimator_warms_up_after_min_samples_s() -> None:
    est = EwmaVolatilityEstimator(SigmaConfig(min_samples_s=10, sample_interval_s=1, jump_guard=False))
    for i in range(10):
        snap = est.update(Decimal("100"), _ts(float(i)))
        assert snap.ready is False
    snap = est.update(Decimal("100.1"), _ts(10.0))
    assert snap.ready is True
    assert snap.effective_samples_s >= 10


def test_constant_price_near_zero_sigma() -> None:
    est = _warm_estimator(min_samples_s=5.0, prices=[100.0] * 20)
    snap = est.snapshot()
    assert snap.ready is True
    assert snap.sigma is not None
    assert snap.sigma < 1e-6


def test_increasing_returns_positive_sigma() -> None:
    est = _warm_estimator(min_samples_s=5.0)
    snap = est.snapshot()
    assert snap.sigma is not None
    assert snap.sigma > 0


def test_timestamp_gap_scales_sigma_with_dt() -> None:
    cfg = SigmaConfig(min_samples_s=1, sample_interval_s=1, jump_guard=False, half_life_s=30)
    est_a = EwmaVolatilityEstimator(cfg)
    est_b = EwmaVolatilityEstimator(cfg)

    est_a.update(Decimal("100"), _ts(0))
    est_a.update(Decimal("101"), _ts(1))

    est_b.update(Decimal("100"), _ts(0))
    est_b.update(Decimal("101"), _ts(2))

    snap_a = est_a.snapshot()
    snap_b = est_b.snapshot()
    assert snap_a.sigma is not None and snap_b.sigma is not None
    # Same log return over longer dt => lower per-sqrt-second sigma.
    assert snap_b.sigma < snap_a.sigma


def test_non_unit_interval_requires_explicit_dt_scaling() -> None:
    est = EwmaVolatilityEstimator(SigmaConfig(min_samples_s=2, sample_interval_s=2, jump_guard=False))
    est.update(Decimal("100"), _ts(0))
    est.update(Decimal("101"), _ts(2))
    est.update(Decimal("102"), _ts(4))
    snap = est.snapshot()
    assert snap.sample_count == 2
    assert snap.effective_samples_s == pytest.approx(4.0)
    assert snap.sigma_units == SIGMA_UNITS_PER_SQRT_SECOND


def test_jump_guard_trips_on_large_return() -> None:
    est = EwmaVolatilityEstimator(
        SigmaConfig(min_samples_s=1, sample_interval_s=1, jump_guard=True, jump_threshold_sigma=2.0)
    )
    est.update(Decimal("100"), _ts(0))
    est.update(Decimal("100.5"), _ts(1))
    before = est.snapshot().sigma
    assert before is not None
    snap = est.update(Decimal("150"), _ts(2))
    assert snap.jump_guard_tripped is True
    assert snap.reject_reason == "jump_guard_tripped"
    assert snap.sigma == before


def test_jump_guard_status_in_output() -> None:
    est = EwmaVolatilityEstimator(
        SigmaConfig(min_samples_s=1, sample_interval_s=1, jump_guard=True, jump_threshold_sigma=1.0)
    )
    est.update(Decimal("100"), _ts(0))
    est.update(Decimal("100.01"), _ts(1))
    snap = est.update(Decimal("200"), _ts(2))
    assert snap.jump_guard_tripped is True


def test_seed_observations_rejects_duplicates_and_future() -> None:
    est = EwmaVolatilityEstimator(SigmaConfig(min_samples_s=2, sample_interval_s=1, jump_guard=False))
    now = _ts(5)
    result = est.seed_observations(
        [(Decimal("100"), _ts(0)), (Decimal("100"), _ts(0)), (Decimal("101"), _ts(10))],
        now_ts=now,
    )
    assert result.rejected_duplicate == 1
    assert result.rejected_future == 1
    assert result.accepted == 1


def test_ewma_lambda_formula() -> None:
    cfg = SigmaConfig(half_life_s=30)
    lam = cfg.ewma_lambda()
    assert lam == pytest.approx(math.exp(-math.log(2) / 30))
