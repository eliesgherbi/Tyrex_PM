"""V1-a: every registered strategy replays a recorded window through the one replay path.

Skipped without the recorded warehouse. Each kind runs with the parameters of its shipped
run config (tests/support/strategy_configs.py) under the default scenario.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from support.strategy_configs import PARAMETERS, protection_for, strategy_config

pytest.importorskip("pyarrow")

ROOT = Path(__file__).resolve().parents[2]
WAREHOUSE = ROOT / "var" / "warehouse_btc5m"
COLLECTION = "380e44875c8e483aa4f76e17cab06c02"
MARKET = "polymarket:condition:0x658335836e89f7c2bc7fb0d801dfffabe2b06532718afcc3418c541bfb87f241"

pytestmark = pytest.mark.skipif(
    not (WAREHOUSE / "collections" / COLLECTION).exists(),
    reason="recorded warehouse collection not present",
)


@pytest.fixture(scope="module")
def window():
    from tyrex_pm.data_warehouse.query.reader import Reader
    from tyrex_pm.research.strategy_replay import WindowCatalog, load_window

    return load_window(WindowCatalog(Reader(WAREHOUSE, COLLECTION)), MARKET)


@pytest.mark.parametrize("kind", sorted(PARAMETERS))
def test_every_strategy_replays_a_recorded_window(window, kind: str) -> None:
    from tyrex_pm.research.replay.config import load_scenario
    from tyrex_pm.research.strategy_replay import decision_digest, run_window
    from tyrex_pm.research.strategy_replay.run import ReplayExecutionPolicy

    config = strategy_config(kind)
    scenario = load_scenario(ROOT / "config" / "research" / "scenarios" / "friction_v3.yaml")
    policy = ReplayExecutionPolicy(protection=protection_for(kind, config))
    out = run_window(
        window, strategy_config=config, scenario=scenario, strategy_kind=kind,
        execution_policy=policy,
    )
    assert out["decisions"], kind
    assert not out.get("unsupported_intents"), out.get("unsupported_intents")
    # Every attempt the strategy made got an outcome back (no orphan attempts).
    assert all(attempt.get("outcome") for attempt in out["attempts"]), out["attempts"]
    assert decision_digest(out)
