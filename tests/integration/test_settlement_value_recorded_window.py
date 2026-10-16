"""End-to-end settlement_value replay on one recorded window (skipped without data).

Checks the M1 contract: the real driver runs through the real runtime stack under both
scenarios, outcomes are proxy-labelled, accounting is consistent and reruns reproduce
the same decision digest.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

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

    catalog = WindowCatalog(Reader(WAREHOUSE, COLLECTION))
    return load_window(catalog, MARKET)


def run(window, scenario_name: str):
    from tyrex_pm.research.replay.config import load_scenario
    from tyrex_pm.research.strategy_replay import run_window
    from tyrex_pm.strategies.settlement_value.schema import (
        settlement_value_config_from_parameters,
    )

    cfg = settlement_value_config_from_parameters(
        {"quality": {"accepted_k_qualities": ["CONFIRMED_CANONICAL", "PROVISIONAL"]}}
    )
    scenario = load_scenario(ROOT / "config" / "research" / "scenarios" / f"{scenario_name}.yaml")
    return run_window(window, strategy_config=cfg, scenario=scenario)


def test_recorded_window_end_to_end_and_reproducible(window) -> None:
    from tyrex_pm.research.strategy_replay import decision_digest

    first = run(window, "favorable")
    assert first["seals"] and first["seals"][0]["quality"] == "PROVISIONAL"
    assert first["label"]["label_type"] == "proxy_outcome"
    assert len(first["orders"]) <= 1  # one entry episode per window
    # Decisions are chronological in virtual time and never precede the range.
    times = [d["at_ns"] for d in first["decisions"]]
    assert times == sorted(times) and times[0] >= window.range_start_ns
    econ = first["economics"]
    if econ["filled"]:
        assert Decimal(econ["debit"]) <= Decimal("5")
        assert Decimal(econ["filled_net_shares"]) <= Decimal(econ["filled_gross_shares"])
        assert Decimal(econ["vwap"]) <= Decimal(first["orders"][0]["limit_price"])
    again = run(window, "favorable")
    assert decision_digest(again) == decision_digest(first)
    friction = run(window, "friction")
    assert friction["scenario_name"] == "friction"
    assert decision_digest(friction) != decision_digest(first)


def test_arrow_loader_matches_reader_loader() -> None:
    from tyrex_pm.data_warehouse.query.reader import Reader
    from tyrex_pm.research.strategy_replay import WindowCatalog, load_window

    catalog = WindowCatalog(Reader(WAREHOUSE, COLLECTION))
    arrow = load_window(catalog, MARKET, method="arrow")
    reader = load_window(catalog, MARKET, method="reader")
    assert arrow.events == reader.events
    assert arrow.source_fingerprint == reader.source_fingerprint


def test_lazy_book_publication_is_decision_equivalent(window) -> None:
    from tyrex_pm.research.replay.config import load_scenario
    from tyrex_pm.research.strategy_replay import run_window
    from tyrex_pm.strategies.settlement_value.schema import (
        settlement_value_config_from_parameters,
    )

    cfg = settlement_value_config_from_parameters(
        {
            "quality": {"accepted_k_qualities": ["CONFIRMED_CANONICAL", "PROVISIONAL"]},
            "entry": {"limit_buffer_ticks": 1},
        }
    )
    scenario = load_scenario(ROOT / "config" / "research" / "scenarios" / "friction.yaml")
    eager = run_window(window, strategy_config=cfg, scenario=scenario, eager_books=True)
    lazy = run_window(window, strategy_config=cfg, scenario=scenario)

    def strip(rows):
        return [{k: v for k, v in row.items() if k != "intent_id"} for row in rows]

    assert strip(eager["decisions"]) == strip(lazy["decisions"])
    assert eager["engine"]["ledger"] == lazy["engine"]["ledger"]
    assert lazy["timing"]["feed_counts"]["book_publications"] < (
        eager["timing"]["feed_counts"]["book_publications"]
    )
    for fill in (e for e in lazy["engine"]["ledger"] if e["event"] == "fill"):
        assert Decimal(str(fill["price"])) <= Decimal(lazy["orders"][0]["limit_price"])
