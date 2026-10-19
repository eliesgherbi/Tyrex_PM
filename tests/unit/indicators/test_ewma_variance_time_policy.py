"""EWMA variance time policies (moved from the notebook 06 study tests, plan P1).

per_sample_v0 is the original estimator and the default; elapsed_variance_v1 keeps
variance per second with an elapsed-time half-life. Both are selected through strategy
configuration -> IndicatorSpec -> sigma_config_from_spec, the path live and replay share.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tyrex_pm.indicators.ewma_volatility import (
    ELAPSED_VARIANCE_V1,
    PER_SAMPLE_V0,
    EwmaVolatilityEstimator,
    SigmaConfig,
)
from tyrex_pm.indicators.graph import IndicatorGraph, sigma_config_from_spec
from tyrex_pm.strategies.settlement_value.plugin import ewma_vol_spec
from tyrex_pm.strategies.settlement_value.schema import settlement_value_config_from_parameters

START = datetime(2026, 10, 19, tzinfo=timezone.utc)


def estimator(policy=ELAPSED_VARIANCE_V1, **kwargs):
    return EwmaVolatilityEstimator(SigmaConfig(
        variance_time_policy=policy, min_samples_s=0, sigma_floor=0, jump_guard=False,
        **kwargs))


def test_elapsed_variance_has_consistent_units_across_irregular_intervals():
    est = estimator(half_life_s=10)
    est._commit_variance(r_t=.01, dt_s=1, ts=START, price=Decimal(100))
    est._commit_variance(r_t=.02, dt_s=4, ts=START + timedelta(seconds=4), price=Decimal(102))
    # Both observations estimate .0001 variance / second.
    assert est.snapshot().sigma == pytest.approx(.01)
    explained = est.explain()
    assert explained["last_dt_s"] == 4
    assert explained["previous_variance"] == pytest.approx(.0001)
    assert explained["variance_units"] == "log_return_variance_per_second"


def test_half_life_means_elapsed_seconds_and_zero_returns_decay():
    est = estimator(half_life_s=10)
    est.update("100", START)
    initial = est.update("101", START + timedelta(seconds=1)).sigma
    final = est.update("101", START + timedelta(seconds=11)).sigma
    assert final == pytest.approx(initial * 2 ** -.5)
    assert est.snapshot().last_update_ts == START + timedelta(seconds=11)
    assert est.explain()["last_return"] == 0


def test_original_policy_is_the_default_and_unchanged():
    assert SigmaConfig().variance_time_policy == PER_SAMPLE_V0
    est = estimator(PER_SAMPLE_V0)
    est.update("100", START)
    initial = est.update("101", START + timedelta(seconds=1)).sigma
    assert est.update("101", START + timedelta(seconds=11)).sigma == initial


def test_elapsed_jump_guard_scales_with_interval():
    est = EwmaVolatilityEstimator(SigmaConfig(
        variance_time_policy=ELAPSED_VARIANCE_V1, min_samples_s=0,
        sigma_floor=0, jump_threshold_sigma=4))
    est.update("100", START)
    est.update("101", START + timedelta(seconds=1))
    result = est.update("106", START + timedelta(seconds=10))
    assert not result.jump_guard_tripped


def test_gap_does_not_create_samples_and_flat_bootstrap_stays_unready():
    est = estimator()
    est.update("100", START)
    result = est.update("100", START + timedelta(seconds=20))
    assert not result.ready and result.sample_count == 0


def test_floor_duplicate_and_reset_invariants():
    est = EwmaVolatilityEstimator(SigmaConfig(
        variance_time_policy=ELAPSED_VARIANCE_V1, sigma_floor=.0001, min_samples_s=0))
    est.update("100", START)
    est.update("100.01", START + timedelta(seconds=1))
    est.update("100.01", START + timedelta(seconds=100))
    assert est.snapshot().sigma >= .0001
    count = est.snapshot().sample_count
    est.update("100.02", START + timedelta(seconds=100))
    assert est.snapshot().sample_count == count
    est.reset()
    assert est.explain()["last_dt_s"] is None and est.snapshot().sigma is None


def test_unknown_policy_is_rejected():
    with pytest.raises(ValueError):
        SigmaConfig(variance_time_policy="enhanced").validate()


def test_strategy_config_selects_the_policy_through_the_shared_spec_path():
    default = settlement_value_config_from_parameters({})
    corrected = settlement_value_config_from_parameters(
        {"volatility": {"variance_time_policy": "elapsed_variance_v1"}})
    default_spec, corrected_spec = ewma_vol_spec(default.volatility), ewma_vol_spec(
        corrected.volatility)
    # The original policy is not emitted: existing indicator ids are unchanged.
    assert "variance_time_policy" not in default_spec.param_map
    assert default_spec.instance_id != corrected_spec.instance_id
    assert sigma_config_from_spec(default_spec).variance_time_policy == PER_SAMPLE_V0
    assert sigma_config_from_spec(corrected_spec).variance_time_policy == ELAPSED_VARIANCE_V1
    with pytest.raises(Exception):
        settlement_value_config_from_parameters(
            {"volatility": {"variance_time_policy": "enhanced"}})


def test_reconnect_rebuilds_the_estimator_with_the_same_policy():
    spec = ewma_vol_spec(settlement_value_config_from_parameters(
        {"volatility": {"variance_time_policy": "elapsed_variance_v1"}}).volatility)
    graph = IndicatorGraph((spec,))
    graph.invalidate_source(spec.source)
    explained = graph.explain()[spec.instance_id]
    assert explained["variance_time_policy"] == ELAPSED_VARIANCE_V1
    assert explained["tape_age_ms"] is None  # no trade since the reset
