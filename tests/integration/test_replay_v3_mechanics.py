"""Replay v3 on one recorded window with fills (skipped without data).

* P3 regression anchor: the v3 runner with the v2 order contract
  (``ReplayExecutionPolicy.legacy_v2``) reproduces the saved v2 decisions, fills and
  economics exactly; the remaining v2 -> v3 differences come from the order contract.
* P2: the amount-denominated planner order spends at most capped notional / (1 + reserve).
* P4/P6/P7 mechanics on real recorded books: retry after zero fills and after pre-send
  gate refusals, thesis-stop exit, re-entry after a confirmed sale, protection TP, residual
  hold at the exit cutoff.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("pyarrow")

ROOT = Path(__file__).resolve().parents[2]
WAREHOUSE = ROOT / "var" / "warehouse_btc5m"
COLLECTION = "380e44875c8e483aa4f76e17cab06c02"
MARKET = "polymarket:condition:0xfb7d8502d6c7d23ddbaa68240b0bfd23eea6be11333da522698e49ac671fc511"
FIXTURE = ROOT / "tests" / "fixtures" / "replay" / "v2_reference_btc-updown-5m-1791302400.json"
BASE = {"quality": {"accepted_k_qualities": ["CONFIRMED_CANONICAL", "PROVISIONAL"]},
        "entry": {"limit_buffer_ticks": 1, "min_edge_per_share": "0"}}

pytestmark = pytest.mark.skipif(
    not (WAREHOUSE / "collections" / COLLECTION).exists(),
    reason="recorded warehouse collection not present",
)


@pytest.fixture(scope="module")
def loaded():
    from tyrex_pm.data_warehouse.query.reader import Reader
    from tyrex_pm.research.strategy_replay import WindowCatalog, load_window

    catalog = WindowCatalog(Reader(WAREHOUSE, COLLECTION))
    window = load_window(catalog, MARKET)
    return window, catalog.clock_observations_for(window.range_start_ns, window.range_end_ns)


def scenario(name: str, **overrides):
    from tyrex_pm.research.replay.config import load_scenario

    base = load_scenario(ROOT / "config" / "research" / "scenarios" / f"{name}.yaml")
    return replace(base, **overrides) if overrides else base


def config(**extra):
    from tyrex_pm.strategies.settlement_value.schema import (
        settlement_value_config_from_parameters,
    )

    params = {k: dict(v) for k, v in BASE.items()}
    for key, value in extra.items():
        params.setdefault(key, {}).update(value)
    return settlement_value_config_from_parameters(params)


def replay(loaded, cfg, scen, policy=None):
    from tyrex_pm.research.strategy_replay import run_window

    window, clock = loaded
    kwargs = {} if policy is None else {"execution_policy": policy}
    return run_window(window, strategy_config=cfg, scenario=scen, clock_observations=clock,
                      **kwargs)


def outcomes(result):
    return [(a["role"], a.get("episode_seq"), a["outcome"], a.get("outcome_reason"),
             a.get("exit_reason")) for a in result["attempts"]]


@pytest.mark.parametrize("scen", ["favorable", "friction"])
def test_v3_with_the_v2_contract_reproduces_v2_exactly(loaded, scen) -> None:
    from tyrex_pm.research.strategy_replay.run import RUNNER_VERSION, ReplayExecutionPolicy

    reference = json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"][scen]
    result = replay(loaded, config(), scenario(scen), ReplayExecutionPolicy.legacy_v2())
    assert result["runner_version"] == RUNNER_VERSION == "strategy_closed_loop_v3"
    decisions = [[d.get("at_ns"), d.get("action"), d.get("reason"), d.get("chosen_side"),
                  d.get("intended_gross_shares"), d.get("max_price")] for d in result["decisions"]]
    fills = [[e.get("at_ns"), str(e.get("quantity")), str(e.get("price")), str(e.get("fee")),
              str(e.get("net_quantity"))]
             for e in result["engine"]["ledger"] if e.get("event") == "fill"]
    assert decisions == reference["decisions"]
    assert fills == [[f[0], *map(str, f[1:])] for f in reference["fills"]]
    for key, value in reference["economics"].items():
        assert str(result["economics"][key]) == str(value), key


def test_planner_amount_contract_caps_the_spend(loaded) -> None:
    result = replay(loaded, config(), scenario("favorable"))
    (attempt,) = result["attempts"]
    spend = Decimal(attempt["spend_amount"])
    assert spend <= Decimal("5") / Decimal("1.02")
    assert Decimal(result["economics"]["debit"]) <= spend
    assert result["provenance"]["execution_contract"]["source"]["digest"]
    assert result["provenance"]["execution_policy"]["order_contract"] == "planner_amount"


def test_retry_after_zero_fills_is_bounded(loaded) -> None:
    cfg = config(entry={"limit_buffer_ticks": 0}, retry={"enabled": True})
    result = replay(loaded, cfg, scenario("favorable", adverse_price_ticks=1))
    assert outcomes(result) == [("ENTRY", 1, "ZERO_FILL", "unfilled_cancelled", None)] * 3
    assert result["decisions"][-1]["reason"] == "RETRY_ATTEMPTS_EXHAUSTED"
    assert result["economics"]["entry_posts"] == 3 and not result["economics"]["filled"]


def test_gate_refusal_is_not_sent_and_retried(loaded) -> None:
    from tyrex_pm.research.strategy_replay.run import ReplayExecutionPolicy

    result = replay(loaded, config(retry={"enabled": True}),
                    scenario("favorable", order_delay_ms=300, final_gate_delay_ms=300),
                    ReplayExecutionPolicy(max_book_age_ms=1))
    assert [o[2:4] for o in outcomes(result)] == [("NOT_SENT", "book_stale")] * 3
    assert result["orders"] == [] and result["economics"]["entry_posts"] == 0


def test_thesis_stop_then_reentry_after_confirmed_sale(loaded) -> None:
    cfg = config(
        exit={"policy": "managed_exit", "thesis_stop_enabled": True,
              "thesis_stop_probability": 0.999, "exit_cutoff_tau_s": 15},
        reentry={"max_trades_per_window": 2, "rearm": "cooldown", "cooldown_s": 5,
                 "lock_after_thesis_stop": False, "last_entry_tau_s": 31},
    )
    result = replay(loaded, cfg, scenario("favorable"))
    steps = outcomes(result)
    assert steps[0][:2] == ("ENTRY", 1) and steps[1][2] == "FLAT"
    assert steps[1][4] == "THESIS_STOP" and steps[2][:2] == ("ENTRY", 2)
    assert result["economics"]["filled_episodes"] == 2
    assert Decimal(result["economics"]["sold_shares"]) > 0
    # Once the exit budget is spent the residual is held to resolution, never top-up.
    assert [h["reason"] for h in result["holds"]] == ["EXIT_CUTOFF_RESIDUAL_HOLD"]


def test_protection_take_profit_runs_through_exit_arbitration(loaded) -> None:
    from tyrex_pm.protection.spec import protection_spec_from_mapping
    from tyrex_pm.research.strategy_replay.run import ReplayExecutionPolicy

    spec = protection_spec_from_mapping({"mark": "bid", "size_mode": "full",
                                         "take_profit": {"style": "reactive_fak",
                                                         "absolute": "0.02"}})
    cfg = config(exit={"policy": "managed_exit", "exit_cutoff_tau_s": 15})
    result = replay(loaded, cfg, scenario("favorable"), ReplayExecutionPolicy(protection=spec))
    assert [p["event"] for p in result["protection_events"]] == ["armed", "triggered"]
    assert outcomes(result)[-1][2:] == ("FLAT", None, "PROTECTION_TP")
    assert result["decisions"][-1]["reason"] == "MAX_TRADES_PER_WINDOW"
    with pytest.raises(ValueError, match="managed_exit"):
        replay(loaded, config(), scenario("favorable"), ReplayExecutionPolicy(protection=spec))
