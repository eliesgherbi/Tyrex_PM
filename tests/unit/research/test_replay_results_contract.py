"""Replay results keep the market's own clock, real positions and one run per input."""

from __future__ import annotations

import json
from pathlib import Path

from tyrex_pm.research.workflows.api import call
from tyrex_pm.research.workflows.artifacts import write_json
from tyrex_pm.research.workflows.jobs import part_path
from tyrex_pm.research.workflows.results import execution_state

MARKET_START = 1_790_869_500_000_000_000
NS = 1_000_000_000


def _job(root: Path, parts: dict[str, dict], traces: dict[str, dict] | None = None) -> None:
    job = root / "jobs" / "job1"
    folder = job / "parts"
    folder.mkdir(parents=True)
    manifest = {}
    for unit, body in parts.items():
        write_json(part_path(folder, unit), {"unit_id": unit, **body})
        manifest[unit] = {"sha256": "ignored"}
        if traces and unit in traces:
            write_json(part_path(folder, unit, ".decisions.json"), traces[unit])
    write_json(folder / "manifest.json", {"parts": manifest})
    write_json(job / "job.json", {"job_id": "job1", "experiment_id": "exp", "plan_digest": "p"})
    write_json(job / "status.json", {"state": "completed", "expected": 1, "completed": 1})
    (root / "experiments").mkdir(exist_ok=True)
    write_json(
        root / "experiments" / "exp.json",
        {
            "mode": "replay",
            "selection_id": "set1",
            "configurations": [{"name": "Mine", "fingerprint": "f"}],
        },
    )
    (root / "selections").mkdir(exist_ok=True)
    write_json(
        root / "selections" / "set1.json",
        {
            "selection_id": "set1",
            "name": "Sample",
            "included": [
                {
                    "market_id": "m1",
                    "window_start_ns": MARKET_START,
                    "window_end_ns": MARKET_START + 300 * NS,
                }
            ],
        },
    )


def test_earlier_parts_label_the_market_not_the_loaded_range(tmp_path: Path) -> None:
    # Before market_interval_v1 the part stored the loaded range: 300 s of warm-up first.
    _job(
        tmp_path,
        {
            "m1-0-friction": {
                "market_id": "m1",
                "configuration_name": "Mine",
                "qualified": True,
                "entered": False,
                "filled": False,
                "window_start_ns": MARKET_START - 300 * NS,
                "window_end_ns": MARKET_START + 330 * NS,
            }
        },
    )
    opened = call("results.open", {"job_id": "job1"}, root=tmp_path)
    assert opened["ok"], opened
    row = opened["result"]["windows"][0]
    assert row["window_start_ns"] == MARKET_START
    assert row["window_end_ns"] == MARKET_START + 300 * NS
    assert row["range_start_ns"] == MARKET_START - 300 * NS
    assert row["window_clock"] == "saved_selection"
    assert row["execution_state"] == "no_entry"


def test_a_filled_earlier_part_recovers_its_position_from_the_saved_ledger(
    tmp_path: Path,
) -> None:
    trace = {
        "orders": [{"order_id": "o1", "side": "UP"}],
        "attempts": [{"role": "ENTRY", "outcome": "FILLED", "side": "UP"}],
        "ledger": [
            {"event": "arrived", "order_id": "o1"},
            {
                "event": "fill",
                "order_id": "o1",
                "side": "BUY",
                "quantity": "13.58",
                "net_quantity": "13.0",
                "price": "0.36",
                "fee": "0.2",
            },
        ],
    }
    _job(
        tmp_path,
        {
            "m1-0-friction": {
                "market_id": "m1",
                "configuration_name": "Mine",
                "qualified": False,
                "entered": True,
                "filled": True,
                "realized_pnl": "-4.89",
            }
        },
        {"m1-0-friction": trace},
    )
    opened = call("results.open", {"job_id": "job1"}, root=tmp_path)
    row = opened["result"]["windows"][0]
    assert row["position_source"] == "saved_trace_ledger"
    assert row["position"]["side"] == "UP"
    assert row["position"]["filled_gross_shares"] == "13.58"
    assert row["execution_state"] == "filled"
    assert opened["result"]["summary"]["execution_states"] == {"filled": 1}


def test_execution_state_keeps_request_fill_and_exit_apart() -> None:
    assert execution_state({"entered": False}) == "no_entry"
    assert (
        execution_state({"entered": True, "attempt_summary": [{"outcome": "NOT_SENT"}]})
        == "not_sent"
    )
    assert execution_state({"entered": True, "filled": False, "attempt_summary": []}) == "no_fill"
    partial = {"entered": True, "filled": True, "attempt_summary": [{"outcome": "PARTIAL"}]}
    assert execution_state(partial) == "partial"
    exited = {"entered": True, "filled": True, "position": {"sold_shares": "5"}}
    assert execution_state(exited) == "exited"


def test_trace_tags_each_ledger_event_with_its_leg() -> None:
    from tyrex_pm.research.workflows.worker import _compact_trace

    trace = _compact_trace(
        {
            "decisions": [],
            "engine": {
                "ledger": [
                    {"event": "fill", "instrument_id": "up-iid", "side": "BUY"},
                    {"event": "arrived", "intent": {"instrument_id": "down-iid"}},
                ]
            },
        },
        window_start_ns=MARKET_START,
        range_start_ns=MARKET_START - 300 * NS,
        legs={"up-iid": "UP", "down-iid": "DOWN"},
    )
    assert [event["leg"] for event in trace["ledger"]] == ["UP", "DOWN"]
    assert trace["range_start_ns"] == MARKET_START - 300 * NS
    assert trace["window_clock_contract"] == "market_interval_v1"


def _preview(root: Path, monkeypatch) -> str:
    monkeypatch.setattr(
        "tyrex_pm.research.workflows.recipes.verify_saved_catalog",
        lambda saved: [row["market_id"] for row in saved["included"]],
    )
    caps = call("strategy.capabilities", {}, root=root)["result"]
    parameters = caps["bases"][0]["parameters"]
    parameters["quality"]["accepted_k_qualities"] = ["CONFIRMED_CANONICAL", "PROVISIONAL"]
    parameters["quality"]["require_verified_settlement_spec"] = False
    preview = call(
        "replay.preview",
        {
            "selection_id": "set1",
            "name": "same inputs",
            "configurations": [{"name": "base", "parameters": parameters}],
        },
        root=root,
    )
    assert preview["ok"], preview
    return preview["result"]["experiments"][0]["experiment_id"]


def _stub_spawn(monkeypatch) -> None:
    monkeypatch.setattr("tyrex_pm.research.workflows.jobs._spawn", lambda job_dir: None)
    monkeypatch.setattr(
        "tyrex_pm.research.workflows.jobs._wait_until_owned",
        lambda job_dir, generation, timeout: {"state": "preparing", "generation": generation},
    )


def _set(root: Path) -> None:
    (root / "selections").mkdir(parents=True)
    (root / "selections" / "set1.json").write_text(
        json.dumps(
            {
                "selection_id": "set1",
                "name": "Sample",
                "digest": "abc",
                "recipe": "eligible_windows",
                "research_policy": "exploratory",
                "included": [{"market_id": "m1", "collection": "c", "warehouse": "w"}],
                "excluded": [],
                "manifests": [],
            }
        ),
        encoding="utf-8",
    )


def test_an_identical_second_preview_cannot_start_a_parallel_job(
    tmp_path: Path, monkeypatch
) -> None:
    _set(tmp_path)
    _stub_spawn(monkeypatch)
    first = _preview(tmp_path, monkeypatch)
    second = _preview(tmp_path, monkeypatch)
    assert first != second
    started = call("job.start", {"experiment_id": first}, root=tmp_path)
    assert started["ok"], started
    refused = call("job.start", {"experiment_id": second}, root=tmp_path)
    assert refused["ok"] is False
    assert refused["error"]["code"] == "identical_replay_active"
    assert refused["error"]["fields"]["job_id"] == started["result"]["job_id"]


def test_history_lists_started_jobs_and_counts_previews_apart(tmp_path: Path, monkeypatch) -> None:
    _set(tmp_path)
    _stub_spawn(monkeypatch)
    started = _preview(tmp_path, monkeypatch)
    _preview(tmp_path, monkeypatch)
    call("job.start", {"experiment_id": started}, root=tmp_path)
    history = call("replay.history", {}, root=tmp_path)["result"]
    assert history["total"] == 1
    assert history["runs"][0]["experiment_id"] == started
    assert history["previews_without_job"] == 1
    assert history["strategies"][0]["runs"] == 1
    previews = call("replay.history", {"status": "previewed"}, root=tmp_path)["result"]
    assert previews["total"] == 1


def test_capabilities_label_every_field_with_a_unit(tmp_path: Path) -> None:
    caps = call("strategy.capabilities", {}, root=tmp_path)["result"]
    fields = {item["key"]: item for item in caps["fields"]}
    assert fields["entry.tau_min_s"]["unit"] == "seconds"
    assert fields["sizing.max_debit_usdc"]["unit"] == "USDC"
    assert all(item["label"][:1].isupper() for item in caps["fields"])
    labels = [item["label"] for item in caps["bases"]]
    assert len(labels) == len(set(labels))
    friction = next(item for item in caps["scenarios"] if item["name"] == "friction")
    execution = {item["key"]: item for item in friction["fields"]}
    assert execution["order_delay_ms"]["unit"] == "ms"
    assert execution["order_delay_ms"]["integer"] is True


def test_a_saved_configuration_becomes_a_base(tmp_path: Path) -> None:
    caps = call("strategy.capabilities", {}, root=tmp_path)["result"]
    parameters = caps["bases"][0]["parameters"]
    parameters["quality"]["accepted_k_qualities"] = ["CONFIRMED_CANONICAL", "PROVISIONAL"]
    parameters["quality"]["require_verified_settlement_spec"] = False
    saved = call(
        "configuration.save",
        {
            "name": "Wider band",
            "parameters": parameters,
            "scenario": "favorable",
            "research_policy": "exploratory",
        },
        root=tmp_path,
    )
    assert saved["ok"], saved
    bases = call("strategy.capabilities", {}, root=tmp_path)["result"]["bases"]
    mine = next(item for item in bases if item["id"] == saved["result"]["base_id"])
    assert mine["group"] == "saved"
    assert mine["scenario"] == "favorable"
    assert mine["fingerprint"] == saved["result"]["fingerprint"]
    refused = call(
        "configuration.save",
        {"name": "No policy", "parameters": parameters, "research_policy": None},
        root=tmp_path,
    )
    assert refused["ok"] is False


def test_experiments_differing_only_by_execution_both_start(tmp_path: Path, monkeypatch) -> None:
    _set(tmp_path)
    _stub_spawn(monkeypatch)
    monkeypatch.setattr(
        "tyrex_pm.research.workflows.recipes.verify_saved_catalog",
        lambda saved: [row["market_id"] for row in saved["included"]],
    )
    caps = call("strategy.capabilities", {}, root=tmp_path)["result"]
    parameters = caps["bases"][0]["parameters"]
    parameters["quality"]["accepted_k_qualities"] = ["CONFIRMED_CANONICAL", "PROVISIONAL"]
    parameters["quality"]["require_verified_settlement_spec"] = False
    preview = call(
        "replay.preview",
        {
            "selection_id": "set1",
            "configurations": [
                {
                    "name": "no tick",
                    "parameters": parameters,
                    "execution": {"adverse_price_ticks": 0},
                },
                {
                    "name": "one tick",
                    "parameters": parameters,
                    "execution": {"adverse_price_ticks": 1},
                },
            ],
        },
        root=tmp_path,
    )["result"]
    first, second = (item["experiment_id"] for item in preview["experiments"])
    assert call("job.start", {"experiment_id": first}, root=tmp_path)["ok"]
    assert call("job.start", {"experiment_id": second}, root=tmp_path)["ok"]


def _row(up, down, reference=None):
    start, end = MARKET_START, MARKET_START + 300 * NS
    track = lambda intervals: {"state": "missing", "partial": True, "intervals": intervals}  # noqa: E731
    return {
        "window_start_ns": start,
        "window_end_ns": end,
        "tracks": {
            "up_book": track(up),
            "down_book": track(down),
            "reference": track(reference or [[start - 900 * NS, end]]),
        },
    }


def test_research_v2_ignores_blips_and_unions_both_legs() -> None:
    from tyrex_pm.research.workflows.recipes import book_profile

    s = MARKET_START
    blip = [[s, s + 100 * NS], [s + 100 * NS + 5_000_000, s + 300 * NS]]  # 5 ms hole
    gap = [[s, s + 50 * NS], [s + 70 * NS, s + 300 * NS]]  # 20 s hole
    profile = book_profile(_row(blip, gap))
    assert profile["holes"] == [[50.0, 20.0]]
    assert profile["longest_hole_s"] == 20.0
    assert profile["final_hole"] is False
    assert round(profile["valid_ratio"], 4) == round(1 - 20 / 300, 4)


def test_research_v2_reasons() -> None:
    from tyrex_pm.research.workflows.recipes import _research_v2_books, _research_v2_reference

    s = MARKET_START
    full = [[s, s + 300 * NS]]
    assert _research_v2_books(_row(full, full)) is None
    # In a 300 s window a single hole over 30 s already loses more than 10%, so the
    # valid-time rule fires first; the 30 s cap only binds on longer windows.
    long_hole = [[s, s + 100 * NS], [s + 135 * NS, s + 300 * NS]]
    assert _research_v2_books(_row(long_hole, full)) == "book_valid_below_90"
    late = [[s, s + 280 * NS]]
    assert _research_v2_books(_row(late, full)) == "book_hole_in_final_60s"
    sparse = [
        [s, s + 100 * NS],
        [s + 125 * NS, s + 150 * NS],
        [s + 175 * NS, s + 200 * NS],
        [s + 225 * NS, s + 300 * NS],
    ]
    assert _research_v2_books(_row(sparse, full)) == "book_valid_below_90"
    # Warm-up before the window is not required; the window itself is.
    assert _research_v2_reference(_row(full, full, reference=[[s, s + 300 * NS]])) is None
    short = [[s + 30 * NS, s + 300 * NS]]
    assert (
        _research_v2_reference(_row(full, full, reference=short)) == "reference_missing_in_window"
    )


def test_k_is_checked_against_the_provider_later() -> None:
    from tyrex_pm.research.workflows.results import k_check

    assert k_check("83810.11475335885", "83810.11475335885") == "confirmed_by_provider"
    assert k_check("83810.1", "83811.1") == "differs_from_provider"
    assert k_check("83810.1", None) == "provider_not_published"
    assert k_check(None, "83810.1") == "not_stored"


def test_research_v2_1_caps_the_longest_hole_at_20_s() -> None:
    from tyrex_pm.research.workflows.recipes import RESEARCH_V2, RESEARCH_V2_1, _research_v2_books

    s = MARKET_START
    full = [[s, s + 300 * NS]]
    hole_25s = [[s, s + 100 * NS], [s + 125 * NS, s + 300 * NS]]
    assert _research_v2_books(_row(hole_25s, full), RESEARCH_V2) is None
    assert _research_v2_books(_row(hole_25s, full), RESEARCH_V2_1) == "book_hole_over_20s"


def _inventory_row(market: str, up: list, down: list) -> dict:
    row = _row(up, down)
    row.update(
        market_id=market,
        collection="c1",
        warehouse="w",
        reference_series="official_reference",
        outcome="up",
        label_source="proxy",
        partition="development",
        block_id="c1:s1",
    )
    present = {"state": "present", "reason": "ok"}
    row["tracks"].update(recording=present, normalization=present, quality=present)
    row["warmup"] = {"state": "missing", "partial": True}
    return row


def test_a_research_v2_1_preview_runs_end_to_end(monkeypatch) -> None:
    from tyrex_pm.research.workflows import recipes

    s = MARKET_START
    full = [[s, s + 300 * NS]]
    hole_25s = [[s, s + 100 * NS], [s + 125 * NS, s + 300 * NS]]
    rows = [_inventory_row("m-clean", full, full), _inventory_row("m-hole", hole_25s, full)]
    monkeypatch.setattr(
        recipes,
        "discover",
        lambda request: {
            "windows": rows,
            "inventory_fingerprint": "f",
            "publication_ids": [],
            "assessed_at": None,
        },
    )
    preview = recipes.resolve_recipe(
        {
            "roots": ["w"],
            "recipe": "eligible_windows",
            "requirements": {"coverage": "research_v2_1"},
        }
    )
    assert [row["market_id"] for row in preview["included"]] == ["m-clean"]
    assert preview["research_policy"] == "replay_research_v2_1"
    reasons = {row["market_id"]: row["reason"] for row in preview["excluded"]}
    assert reasons == {"m-hole": "book_hole_over_20s"}
    profiles = {row["market_id"]: row for row in preview["window_profiles"]}
    assert profiles["m-hole"]["kept"] is False
    assert profiles["m-hole"]["longest_hole_s"] == 25.0
