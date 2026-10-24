"""Replay preview keeps the saved set whole and refuses a silent policy merge."""

from __future__ import annotations

import json
from pathlib import Path

from tyrex_pm.research.workflows.api import call
from tyrex_pm.research.workflows.jobs import part_path


def _selection(root: Path) -> None:
    folder = root / "selections"
    folder.mkdir(parents=True)
    (folder / "set1.json").write_text(
        json.dumps(
            {
                "selection_id": "set1",
                "name": "6 Oct sample",
                "digest": "abc",
                "recipe": "eligible_windows",
                "research_policy": "exploratory",
                "research_limitation": "exploratory inputs",
                "included": [
                    {"market_id": "m-holdout", "collection": "c", "warehouse": "w", "block_id": "b"}
                ],
                "excluded": [],
                "manifests": [{"collection_id": "c", "files": ["kept"], "build_id": "real"}],
            }
        ),
        encoding="utf-8",
    )


def _parameters(root: Path) -> dict:
    caps = call("strategy.capabilities", {}, root=root)
    assert caps["ok"]
    parameters = caps["result"]["bases"][0]["parameters"]
    parameters["quality"]["accepted_k_qualities"] = ["CONFIRMED_CANONICAL", "PROVISIONAL"]
    parameters["quality"]["require_verified_settlement_spec"] = False
    return parameters


def test_capabilities_name_the_supported_adapter(tmp_path: Path) -> None:
    caps = call("strategy.capabilities", {}, root=tmp_path)
    kinds = {item["kind"]: item for item in caps["result"]["strategies"]}
    assert kinds["settlement_value"]["available"] is True
    assert kinds["ask70"]["available"] is False
    assert "entry.min_edge_per_share" in caps["result"]["essentials"]
    names = [item["name"] for item in caps["result"]["scenarios"]]
    assert names[0] == caps["result"]["default_scenario"] == "friction_v3"
    assert {"friction_stress_v1", "friction", "favorable"} <= set(names)


def test_validate_refuses_a_missing_policy_field(tmp_path: Path) -> None:
    parameters = _parameters(tmp_path)
    del parameters["quality"]["accepted_k_qualities"]
    outcome = call(
        "replay.validate",
        {"parameters": parameters, "research_policy": "exploratory", "scenario": "friction"},
        root=tmp_path,
    )
    assert outcome["ok"]
    assert outcome["result"]["valid"] is False
    assert "accepted_k_qualities" in outcome["result"]["errors"][0]


def test_preview_uses_the_whole_set_and_job_start_skips_the_split(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "tyrex_pm.research.workflows.recipes.verify_saved_catalog",
        lambda saved: [row["market_id"] for row in saved["included"]],
    )
    from tyrex_pm.research.workflows import partition

    assert not hasattr(partition, "assert_development_markets")
    monkeypatch.setattr("tyrex_pm.research.workflows.jobs._spawn", lambda job_dir: None)
    monkeypatch.setattr(
        "tyrex_pm.research.workflows.jobs._wait_until_owned",
        lambda job_dir, generation, timeout: {
            "state": "preparing",
            "generation": generation,
            "pid": None,
        },
    )
    _selection(tmp_path)
    parameters = _parameters(tmp_path)
    preview = call(
        "replay.preview",
        {
            "selection_id": "set1",
            "name": "sample replay",
            "configurations": [{"name": "base", "parameters": parameters, "scenario": "friction"}],
        },
        root=tmp_path,
    )
    assert preview["ok"], preview
    result = preview["result"]
    assert result["valid"] is True
    assert result["windows"] == 1
    assert result["units"] == 1
    assert result["holdout"] is None
    assert result["objective"] is None
    stored = json.loads(
        (tmp_path / "experiments" / f"{result['experiments'][0]['experiment_id']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["mode"] == "replay"
    assert stored["units"][0]["parameters"]["quality"]["accepted_k_qualities"] == [
        "CONFIRMED_CANONICAL",
        "PROVISIONAL",
    ]
    started = call(
        "job.start",
        {"experiment_id": result["experiments"][0]["experiment_id"]},
        root=tmp_path,
    )
    assert started["ok"], started
    duplicate = call(
        "job.start",
        {"experiment_id": result["experiments"][0]["experiment_id"]},
        root=tmp_path,
    )
    assert duplicate["ok"] is False
    assert duplicate["error"]["code"] == "job_exists"


def test_validate_refuses_invalid_execution_before_preview(tmp_path: Path) -> None:
    parameters = _parameters(tmp_path)
    cases = (
        ({"order_delay_ms": -1}, "order_delay_ms", "negative"),
        ({"timer_interval_ms": 0}, "timer_interval_ms", "timer_interval"),
        ({"fee_policy.rate": "2"}, "fee_policy.rate", "fee"),
    )
    for execution, field, text in cases:
        outcome = call(
            "replay.validate",
            {
                "parameters": parameters,
                "research_policy": "exploratory",
                "scenario": "friction",
                "execution": execution,
            },
            root=tmp_path,
        )
        assert outcome["ok"], outcome
        assert outcome["result"]["valid"] is False
        assert outcome["result"]["field"] == field
        assert text in outcome["result"]["errors"][0]


def test_validate_fingerprint_includes_execution(tmp_path: Path) -> None:
    parameters = _parameters(tmp_path)
    base = {"parameters": parameters, "research_policy": "exploratory", "scenario": "friction"}
    first = call("replay.validate", {**base, "execution": {}}, root=tmp_path)
    second = call(
        "replay.validate",
        {**base, "execution": {"order_delay_ms": 400}},
        root=tmp_path,
    )
    assert first["result"]["valid"] is True
    assert second["result"]["valid"] is True
    assert first["result"]["fingerprint"] != second["result"]["fingerprint"]
    assert first["result"]["execution"]["order_delay_ms"] == 300
    assert second["result"]["execution"]["order_delay_ms"] == 400


def test_population_summary_is_independent_of_a_ledger_page(tmp_path: Path) -> None:
    from tyrex_pm.research.workflows.artifacts import write_json

    job = tmp_path / "jobs" / "job1"
    parts = job / "parts"
    parts.mkdir(parents=True)
    manifest = {}
    for index in range(15):
        unit = f"u{index}"
        qualified = index < 13
        body = {
            "unit_id": unit,
            "market_id": unit,
            "configuration_name": "Mine",
            "qualified": qualified,
            "entered": not qualified,
            "filled": False,
            "realized_pnl": None,
            "window_start_ns": 1_000_000_000 * (15 - index),
            "window_clock_contract": "market_interval_v1",
            "attempt_summary": []
            if qualified
            else [
                {
                    "role": "ENTRY",
                    "outcome": "ZERO_FILL",
                    "outcome_reason": "unfilled_cancelled",
                    "side": "DOWN",
                }
            ],
        }
        write_json(part_path(parts, unit), body)
        manifest[unit] = {"sha256": "ignored"}
    write_json(parts / "manifest.json", {"parts": manifest})
    write_json(job / "job.json", {"job_id": "job1", "experiment_id": "exp", "plan_digest": "p"})
    write_json(job / "status.json", {"state": "completed", "expected": 15, "completed": 15})
    (tmp_path / "experiments").mkdir()
    write_json(
        tmp_path / "experiments" / "exp.json",
        {"mode": "replay", "configurations": [{"name": "Mine", "fingerprint": "f"}]},
    )
    opened = call(
        "results.open",
        {"job_id": "job1", "ledger": "unqualified", "limit": 2},
        root=tmp_path,
    )
    assert opened["ok"], opened
    summary = opened["result"]["summary"]
    assert summary["windows"] == 15
    assert summary["qualified"] == 13
    assert summary["unqualified"] == 2
    assert summary["qualified_pnl"] is None
    assert summary["attempts"] == 2
    assert len(opened["result"]["windows"]) == 2
    assert len(opened["result"]["series"]) == 15
    first = opened["result"]["series"][0]["window_start_ns"]
    last = opened["result"]["series"][-1]["window_start_ns"]
    assert first < last


def test_compact_trace_keeps_the_decision_clock() -> None:
    from tyrex_pm.research.workflows.worker import _compact_trace

    trace = _compact_trace(
        {
            "decisions": [{"action": "WAIT", "at_ns": 5, "p_up_point": "0.5"}],
            "orders": [],
            "attempts": [{"outcome": "ZERO_FILL", "outcome_reason": "unfilled_cancelled"}],
            "engine": {"ledger": [{"event": "fill", "price": "0.4"}]},
        },
        window_start_ns=1,
        window_end_ns=2,
    )
    assert trace["schema"] == "replay_trace_v3"
    assert trace["decisions"][0]["at_ns"] == 5
    assert trace["window_start_ns"] == 1
    assert trace["ledger"][0]["event"] == "fill"


def test_verified_units_require_a_published_trace(tmp_path: Path) -> None:
    from tyrex_pm.research.workflows.artifacts import file_sha256, write_json
    from tyrex_pm.research.workflows.jobs import verified_unit_ids

    parts = tmp_path / "parts"
    parts.mkdir()
    summary = part_path(parts, "unit-a")
    write_json(summary, {"unit_id": "unit-a"})
    write_json(
        parts / "manifest.json",
        {"parts": {"unit-a": {"sha256": file_sha256(summary), "trace_sha256": "missing"}}},
    )
    assert verified_unit_ids(tmp_path) == set()
    trace = part_path(parts, "unit-a", ".decisions.json")
    write_json(trace, {"schema": "replay_trace_v2"})
    write_json(
        parts / "manifest.json",
        {
            "parts": {
                "unit-a": {"sha256": file_sha256(summary), "trace_sha256": file_sha256(trace)}
            }
        },
    )
    assert verified_unit_ids(tmp_path) == {"unit-a"}
    write_json(parts / "manifest.json", {"parts": {"unit-a": {"sha256": file_sha256(summary)}}})
    assert verified_unit_ids(tmp_path) == {"unit-a"}


def test_part_path_keeps_market_ids_off_windows_filenames(tmp_path: Path) -> None:
    market = "polymarket:condition:0xabc"
    path = part_path(tmp_path, f"{market}-0-friction")
    assert ":" not in path.name
    assert path.suffix == ".json"
    assert part_path(tmp_path, "replay").name == "replay.json"
    other = part_path(tmp_path, f"{market}-1-friction")
    assert other.name != path.name
