from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tyrex_pm.indicators.spec import IndicatorSpec
from tyrex_pm.research.strategy_lab.extreme_cases import (
    ProbabilityStudy,
    StudyIndicatorGraph,
    gap_reason,
)

START = datetime(2026, 10, 19, tzinfo=timezone.utc)


def test_reconnect_preserves_experimental_time_policy():
    spec = IndicatorSpec(name="ewma_vol", source="binance", params=tuple({
        "half_life_s": "30", "min_samples_s": "20", "jump_threshold_sigma": "4",
        "sample_interval_s": "1", "tau_floor_s": "1", "sigma_floor": ".00001",
    }.items()))
    graph = StudyIndicatorGraph((spec,), time_policy="elapsed_variance_v1")
    graph.invalidate_source("binance")
    assert graph._ewma[spec.instance_id].config.variance_time_policy == "elapsed_variance_v1"
    legacy = StudyIndicatorGraph((spec,), time_policy="legacy")
    assert legacy._ewma[spec.instance_id].config.variance_time_policy == "per_sample_v0"


# Estimator invariants moved to tests/unit/indicators/test_ewma_variance_time_policy.py.


@pytest.mark.parametrize("row,reason", [
    ({"up_bid": .1, "up_ask": .12, "p_up_point": .99}, "STUDY_DISAGREEMENT_VETO"),
    ({"up_bid": .5, "up_ask": .52, "p_up_point": .55}, None),
    ({"up_bid": .6, "up_ask": .5, "p_up_point": .55}, "STUDY_COMPARISON_UNAVAILABLE"),
    ({"up_bid": None}, "STUDY_COMPARISON_UNAVAILABLE"),
    ({"up_bid": .1, "up_ask": .12, "p_up_point": float("nan")},
     "STUDY_COMPARISON_UNAVAILABLE"),
])
def test_gap_uses_same_side_midpoint_validates_quotes(row, reason):
    assert gap_reason(row, 9) == reason
    assert gap_reason(row, None) is None


def test_veto_waits_then_releases_without_touching_order_state():
    from tyrex_pm.strategies.context import DecisionContext

    context = DecisionContext(run_id="test", snapshot=None, target_notional=Decimal(5))
    mutable_quote = {"up_bid": .1, "up_ask": .12, "p_up_point": .99}
    observed_calls = []

    def observe(snapshot, ctx, features, tau, row):
        observed_calls.append(tau)
        row.update(mutable_quote)

    driver = SimpleNamespace(_observe=observe)
    agent = SimpleNamespace(_driver=lambda: driver, engine=SimpleNamespace(now=0),
                            window=SimpleNamespace(ref=SimpleNamespace(window_end_ns=100e9)))
    study = ProbabilityStudy(disagreement_pp=9)
    # Deliberately different from raw engine time: use the same corrected snapshot
    # time as the strategy, never a separate raw-receive clock for the gate.
    snapshot = SimpleNamespace(market=SimpleNamespace(event_end=START + timedelta(seconds=100)),
                               observed_at=START + timedelta(seconds=3))
    ready = SimpleNamespace(snapshot=snapshot)
    blocked = study.before(agent, ready, context, None)
    assert not blocked.entry_allowed
    assert blocked.entry_block_reason == "STUDY_DISAGREEMENT_VETO"
    assert context.entry_allowed  # immutable input; no episode or retry budget touched
    mutable_quote.update(p_up_point=.14)
    assert study.before(agent, ready, context, None).entry_allowed
    assert observed_calls == [97, 97]


def test_invalid_recording_and_veto_settings_rejected():
    with pytest.raises(ValueError):
        ProbabilityStudy(trace_level="everything")
    with pytest.raises(ValueError):
        ProbabilityStudy(disagreement_pp=-9)
