"""Set cookbook: recipe files become ordinary filters, percentiles become saved thresholds."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from test_data_library import _scope, build_archive

from tyrex_pm.research.workflows import cookbook, features, recipes
from tyrex_pm.research.workflows.api import call
from tyrex_pm.research.workflows.contracts import WorkflowError


def test_the_cookbook_lists_its_recipes_with_their_defaults() -> None:
    listed = call("cookbook.list", {}, root=Path("unused"))["result"]["recipes"]
    by_id = {item["id"]: item for item in listed}
    assert {"us_open_hour", "high_volatility"} <= set(by_id)
    assert by_id["us_open_hour"]["filters"]["time_zone"] == "America/New_York"
    assert by_id["us_open_hour"]["defaults"] == {
        "daily_time.start": "09:30",
        "daily_time.end": "10:30",
    }
    assert by_id["high_volatility"]["defaults"] == {"quantiles.realised_vol_bp.0": 0.67}


def test_parameters_are_checked_against_the_recipe() -> None:
    with pytest.raises(WorkflowError) as bad_time:
        cookbook.apply_recipe({"id": "us_open_hour", "parameters": {"daily_time.start": "25:00"}})
    assert bad_time.value.code == "invalid_parameter"
    with pytest.raises(WorkflowError) as not_exposed:
        cookbook.apply_recipe({"id": "us_open_hour", "parameters": {"outcome": "up"}})
    assert not_exposed.value.code == "invalid_parameter"
    with pytest.raises(WorkflowError) as changed:
        cookbook.apply_recipe({"id": "us_open_hour", "version": 99})
    assert changed.value.code == "recipe_changed"


def test_a_recipe_resolves_in_new_york_time_with_its_parameters(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse"
    build_archive(warehouse)
    request = {**_scope(warehouse), "recipe": "cookbook", "requirements": {}}
    default = call(
        "data_recipe.resolve", {**request, "cookbook": {"id": "us_open_hour"}}, root=tmp_path
    )
    assert default["ok"], default
    assert default["result"]["candidates"] == []  # no window at 09:30-10:30 New York time
    # m-bad starts Monday 30 Mar 12:00 UTC = 08:00 in New York (daylight time since 8 Mar).
    early = call(
        "data_recipe.resolve",
        {
            **request,
            "cookbook": {
                "id": "us_open_hour",
                "parameters": {"daily_time.start": "07:30", "daily_time.end": "09:00"},
            },
        },
        root=tmp_path,
    )["result"]
    assert [row["market_id"] for row in early["candidates"]] == ["m-bad"]
    record = early["cookbook"]
    assert record["id"] == "us_open_hour" and record["version"] == 1
    assert record["parameters"] == {"daily_time.start": "07:30", "daily_time.end": "09:00"}
    assert record["snapshot"]["filters"]["daily_time"]["start"] == "09:30"  # the file as read


def test_percentiles_become_saved_absolute_thresholds(tmp_path: Path, monkeypatch) -> None:
    warehouse = tmp_path / "warehouse"
    build_archive(warehouse)
    vol = {"m-up": 1.0, "m-down": 2.0, "m-open": 3.0, "m-bad": 4.0, "m-partial": 5.0}
    monkeypatch.setattr(
        features,
        "window_features",
        lambda rows: {
            row["market_id"]: {"realised_vol_bp": vol.get(row["market_id"])} for row in rows
        },
    )
    monkeypatch.setattr(recipes, "_requirement_reason", lambda row, req, recipe: None)
    request = {
        **_scope(warehouse),
        "recipe": "cookbook",
        "cookbook": {"id": "high_volatility"},
        "requirements": {},
    }
    preview = call("data_recipe.resolve", request, root=tmp_path)["result"]
    assert preview["filters"]["conditions"] == {"realised_vol_bp": [3.0, None]}
    assert sorted(row["market_id"] for row in preview["included"]) == [
        "m-bad",
        "m-open",
        "m-partial",
    ]
    resolved = preview["cookbook"]["resolved_quantiles"]["realised_vol_bp"]
    assert resolved == {"percentiles": [0.67, None], "values": [3.0, None], "ranked": 5}
    saved = call(
        "selection.save",
        {**request, "preview_id": preview["preview_id"], "name": "volatile"},
        root=tmp_path,
    )
    assert saved["ok"], saved
    body = json.loads(Path(saved["result"]["path"]).read_text(encoding="utf-8"))
    assert body["recipe"] == "cookbook"
    assert body["cookbook"]["snapshot"]["id"] == "high_volatility"
    assert body["filters"]["conditions"] == {"realised_vol_bp": [3.0, None]}


def test_a_saved_snapshot_is_used_even_after_the_file_changes() -> None:
    record = cookbook.apply_recipe({"id": "us_open_hour"})["record"]
    snapshot = {**record["snapshot"], "version": 1}
    snapshot["filters"] = {**snapshot["filters"], "weekdays": [6, 7]}
    applied = cookbook.apply_recipe({"id": "us_open_hour", "snapshot": snapshot})
    assert applied["filters"]["weekdays"] == [6, 7]


def test_a_custom_set_can_become_a_recipe(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cookbook, "COOKBOOK", tmp_path / "recipes")
    filters = {
        "time_zone": "Europe/Paris",
        "daily_time": {"enabled": True, "start": "22:00", "end": "02:00"},
        "weekdays": [1, 2, 3, 4, 5],
        "picked": ["m1"],
    }
    saved = call(
        "cookbook.save", {"title": "Paris late evening", "filters": filters}, root=tmp_path
    )
    assert saved["ok"], saved
    written = yaml.safe_load((tmp_path / "recipes" / "paris_late_evening.yaml").read_text())
    assert (
        written["filters"]["selection_mode"] == "all_matches" and "picked" not in written["filters"]
    )
    listed = cookbook.recipes()
    assert [item["id"] for item in listed] == ["paris_late_evening"]
    assert {item["path"] for item in listed[0]["parameters"]} == {
        "daily_time.start",
        "daily_time.end",
        "weekdays",
    }
    again = call(
        "cookbook.save", {"title": "Paris late evening", "filters": filters}, root=tmp_path
    )
    assert again["error"]["code"] == "recipe_exists"
    conditioned = call(
        "cookbook.save", {"title": "Up only", "filters": {"outcome": "up"}}, root=tmp_path
    )
    assert conditioned["error"]["code"] == "invalid_recipe"
