"""Replay execution contract v2: the live feature-readiness rule at the replay final gate.

v1 stays the default and keeps every existing identity; v2 is opted into by new
experiments (integration plan 0.2 and decision 8).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from tyrex_pm.indicators.graph import IndicatorGraph
from tyrex_pm.indicators.readiness import (
    EWMA_NOT_READY,
    features_ready_reason,
    required_feature_specs,
)
from tyrex_pm.research.strategy_replay.run import (
    EXECUTION_CONTRACT_V2,
    EXECUTION_CONTRACT_VERSION,
    ReplayExecutionPolicy,
)
from tyrex_pm.strategies.settlement_value.plugin import ewma_vol_spec
from tyrex_pm.strategies.settlement_value.schema import settlement_value_config_from_parameters


def test_v1_identity_is_unchanged_and_v2_is_distinct() -> None:
    v1 = ReplayExecutionPolicy()
    assert v1.execution_contract == EXECUTION_CONTRACT_VERSION
    assert "execution_contract" not in v1.identity()
    v2 = ReplayExecutionPolicy(execution_contract=EXECUTION_CONTRACT_V2)
    assert v2.identity()["execution_contract"] == EXECUTION_CONTRACT_V2
    with pytest.raises(ValueError):
        ReplayExecutionPolicy(execution_contract="replay_execution_contract_v9")


def test_shared_readiness_rule() -> None:
    from tyrex_pm.core.clock import FakeClock

    spec = ewma_vol_spec(settlement_value_config_from_parameters({}).volatility)
    clock = FakeClock(_wall=datetime(2026, 10, 19, tzinfo=timezone.utc))
    graph = IndicatorGraph((spec,), clock=clock)
    required = required_feature_specs(graph.specs)
    assert required == (spec,)
    assert features_ready_reason(graph, required) == EWMA_NOT_READY  # warming up
    assert features_ready_reason(None, required) == EWMA_NOT_READY
    assert features_ready_reason(graph, ()) is True


def test_timeline_replay_uses_the_runs_execution_policy() -> None:
    from tyrex_pm.research.strategy_lab.timelines import _row_policy

    policy = {"reference_availability": "reference_availability_v1",
              "execution_contract": EXECUTION_CONTRACT_V2}
    assert _row_policy(pd.Series({"execution_policy": json.dumps(policy)})) == policy
    assert _row_policy(pd.Series({"execution_policy": "{}"})) is None
    assert _row_policy(pd.Series({"execution_policy": float("nan")})) is None
    assert _row_policy(pd.Series({"config": "{}"})) is None  # older tables
