"""V1-e: the research workspace offers, validates and previews any declared strategy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_replay_preview import _selection

from tyrex_pm.research.workflows.api import call
from tyrex_pm.strategies.registry import get_strategy_plugin, registered_strategy_kinds


def test_every_registered_strategy_is_offered_with_its_own_form(tmp_path: Path) -> None:
    caps = call("strategy.capabilities", {}, root=tmp_path)["result"]
    offered = {item["kind"] for item in caps["strategies"] if item["available"]}
    assert offered == set(registered_strategy_kinds())
    for kind in registered_strategy_kinds():
        result = call("strategy.capabilities", {"strategy_kind": kind}, root=tmp_path)["result"]
        assert result["strategy_kind"] == kind and result["fields"], kind
        keys = {field["key"] for field in result["fields"]}
        sections = {s for group in result["groups"] for s in group["sections"]}
        assert {field["section"] for field in result["fields"]} <= sections, kind
        # The defaults base validates as-is.
        base = result["bases"][0]["parameters"]
        valid = call(
            "replay.validate",
            {"strategy_kind": kind, "parameters": base, "research_policy": "exploratory",
             **({"parameters": _with_policy(base)} if kind == "settlement_value" else {})},
            root=tmp_path,
        )["result"]
        assert valid["valid"] is True, (kind, valid)
        assert keys  # every form has editable fields


def _with_policy(parameters: dict) -> dict:
    parameters["quality"]["accepted_k_qualities"] = ["CONFIRMED_CANONICAL", "PROVISIONAL"]
    parameters["quality"]["require_verified_settlement_spec"] = False
    return parameters


def test_strategies_requiring_protection_get_a_default_block(tmp_path: Path) -> None:
    caps = call("strategy.capabilities", {"strategy_kind": "ask70"}, root=tmp_path)["result"]
    assert caps["protection"]["stop_loss"]["style"] == "stop_market_fak"
    plain = call("strategy.capabilities", {"strategy_kind": "z_gap"}, root=tmp_path)["result"]
    assert plain["protection"] is None


@pytest.mark.parametrize("kind", ["z_gap", "ask70"])
def test_preview_builds_units_for_other_strategies(tmp_path: Path, monkeypatch, kind) -> None:
    monkeypatch.setattr(
        "tyrex_pm.research.workflows.recipes.verify_saved_catalog",
        lambda saved: [row["market_id"] for row in saved["included"]],
    )
    _selection(tmp_path)
    base = call("strategy.capabilities", {"strategy_kind": kind}, root=tmp_path)["result"]
    preview = call(
        "replay.preview",
        {
            "selection_id": "set1",
            "name": f"{kind} replay",
            "configurations": [
                {"name": "base", "strategy_kind": kind,
                 "parameters": base["bases"][0]["parameters"], "scenario": "friction_v3"}
            ],
        },
        root=tmp_path,
    )
    assert preview["ok"] and preview["result"]["valid"], preview
    experiment_id = preview["result"]["experiments"][0]["experiment_id"]
    stored = json.loads((tmp_path / "experiments" / f"{experiment_id}.json").read_text("utf-8"))
    assert stored["strategy_kind"] == kind
    unit = stored["units"][0]
    assert unit["strategy_kind"] == kind
    plugin = get_strategy_plugin(kind)
    assert plugin.load_config(unit["parameters"], "test")  # the worker can load it
    assert ("protection" in unit) is (kind == "ask70")
