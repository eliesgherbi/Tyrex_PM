"""V1-c: a strategy package defined only in tests plugs in without editing the framework.

Registered at test time, it runs through the decision harness (``tyrex-pm eval``), is
offered by the research workspace, and replays a recorded window through the one replay
path (the replay part is skipped without the recorded warehouse).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from support.demo_strategy import KIND, WATCHER_PLUGIN

from tyrex_pm.strategies import registry

ROOT = Path(__file__).resolve().parents[2]
WAREHOUSE = ROOT / "var" / "warehouse_btc5m"
COLLECTION = "380e44875c8e483aa4f76e17cab06c02"
MARKET = "polymarket:condition:0x658335836e89f7c2bc7fb0d801dfffabe2b06532718afcc3418c541bfb87f241"


@pytest.fixture(autouse=True)
def watcher():
    registry.ensure_strategies_registered()
    registry.register_strategy(WATCHER_PLUGIN)
    yield WATCHER_PLUGIN
    registry._REGISTRY.pop(KIND, None)


def test_the_decision_harness_runs_it() -> None:
    from tyrex_pm.lab.evaluate_replay import evaluate_replay, load_lab_pack

    pack = load_lab_pack(ROOT / "tests" / "fixtures" / "lab" / "ask70_books.json")
    results = evaluate_replay(
        plugin=WATCHER_PLUGIN, config=WATCHER_PLUGIN.load_config({}, "test"),
        steps=pack.steps, market=pack.market,
    )
    assert results and all(r.decision.reason_code == "WATCH_ONLY" for r in results)


def test_the_workspace_offers_it(tmp_path: Path) -> None:
    from tyrex_pm.research.workflows.api import call

    caps = call("strategy.capabilities", {"strategy_kind": KIND}, root=tmp_path)["result"]
    assert {f["key"] for f in caps["fields"]} == {"tau_min_s", "tau_max_s",
                                                   "max_clock_uncertainty_ms"}
    offered = {item["kind"] for item in caps["strategies"] if item["available"]}
    assert KIND in offered


@pytest.mark.skipif(
    not (WAREHOUSE / "collections" / COLLECTION).exists(),
    reason="recorded warehouse collection not present",
)
def test_it_replays_a_recorded_window() -> None:
    from tyrex_pm.data_warehouse.query.reader import Reader
    from tyrex_pm.research.replay.config import load_scenario
    from tyrex_pm.research.strategy_replay import WindowCatalog, load_window, run_window

    window = load_window(WindowCatalog(Reader(WAREHOUSE, COLLECTION)), MARKET)
    scenario = load_scenario(ROOT / "config" / "research" / "scenarios" / "friction_v3.yaml")
    out = run_window(
        window, strategy_config=WATCHER_PLUGIN.load_config({}, "test"), scenario=scenario,
        strategy_kind=KIND,
    )
    reasons = {d.get("reason") for d in out["decisions"]}
    assert "WATCH_ONLY" in reasons and not out["orders"]
