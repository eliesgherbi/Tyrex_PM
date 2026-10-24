"""Workflow contracts, pins, guard, profiles, and the real replay fixture."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tyrex_pm.data_warehouse.capture.files import Ownership
from tyrex_pm.data_warehouse.contracts import canonical
from tyrex_pm.data_warehouse.supervisor import collector_stop_matches
from tyrex_pm.execution.process_lock import StateDirectoryLock
from tyrex_pm.research.workflows.api import CATALOG, HANDLERS, call
from tyrex_pm.research.workflows.artifacts import file_sha256
from tyrex_pm.research.workflows.notebook import open_results, resolve_experiment
from tyrex_pm.research.workflows.replay_run import run_closed_loop_fixture

REPO = Path(__file__).resolve().parents[3]


def _split(tmp: Path) -> Path:
    path = tmp / "split.json"
    path.write_text(
        json.dumps({"development": ["dev-market"], "holdout": ["hold-market"]}),
        encoding="utf-8",
    )
    return path


def _window(market: str = "m1") -> dict:
    return {"market_id": market, "collection": "c1", "warehouse": "w", "reference_series": "rtds"}


def test_catalog_lists_operations_not_helpers() -> None:
    assert set(CATALOG) == set(HANDLERS)
    assert "execute_unit" not in CATALOG
    assert call("not.an.operation")["error"]["code"] == "unknown_operation"


def test_prepare_is_not_collection_and_reuses_the_normalizer_lock(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse"
    first = call(
        "library.prepare",
        {"kind": "normalize", "warehouse": str(warehouse), "collection": "c1"},
        root=tmp_path / "wf",
    )
    assert first["ok"] is True
    assert first["result"]["collects_new_observations"] is False
    refused = call(
        "library.prepare",
        {"kind": "collect", "warehouse": str(warehouse), "collection": "c1"},
        root=tmp_path / "wf",
    )
    assert refused["error"]["code"] == "preparation_kind"
    with Ownership(warehouse, "normalizer"):
        busy = call(
            "library.prepare",
            {"kind": "normalize", "warehouse": str(warehouse), "collection": "c1"},
            root=tmp_path / "wf",
        )
    assert busy["ok"] is False
    assert busy["error"]["code"] == "preparation_failed"


def test_duplicate_choice_and_pin_reopen(tmp_path: Path) -> None:
    root = tmp_path / "wf"
    rows = [
        _window("same"),
        {**_window("same"), "collection": "c2"},
        _window("only"),
    ]
    resolved = call("selection.resolve", {"windows": rows}, root=root)
    assert resolved["result"]["requires_choice"] == ["same"]
    assert any(row["market_id"] == "only" for row in resolved["result"]["included"])
    blob = tmp_path / "part.bin"
    blob.write_bytes(b"pinned-bytes")
    saved = call(
        "selection.save",
        {
            "windows": rows,
            "choices": {"same": "c1"},
            "pin_parts": [{"path": str(blob)}],
            "reference_series_policy": {"series": "rtds", "provenance": "ASSUMED"},
            "capture_provenance": [{"capture_id": "cap-1"}],
            "warehouse": str(tmp_path / "empty-warehouse"),
            "collection": "c1",
        },
        root=root,
    )
    assert saved["ok"] is True
    (tmp_path / "later-publication.bin").write_bytes(b"new bytes beside the pin")
    opened = call("selection.open", {"selection_id": saved["result"]["selection_id"]}, root=root)
    assert opened["ok"] is True
    assert opened["result"]["digest"] == saved["result"]["digest"]
    assert opened["result"]["reference_series_policy"]["series"] == "rtds"
    blob.write_bytes(b"changed")
    mismatch = call("selection.open", {"selection_id": saved["result"]["selection_id"]}, root=root)
    assert mismatch["error"]["code"] == "pin_mismatch"
    assert file_sha256(blob) != opened["result"]["digest"]


def test_reference_series_and_capture_stay_separate_choices(tmp_path: Path) -> None:
    root = tmp_path / "wf"
    series = call(
        "selection.resolve",
        {
            "windows": [
                _window("m"),
                {**_window("m"), "reference_series": "polybolt"},
            ]
        },
        root=root,
    )
    assert series["result"]["requires_choice"] == ["m"]
    assert series["result"]["excluded"][0]["reason"] == "ambiguous_reference_series"
    chosen = call(
        "selection.resolve",
        {
            "windows": [
                _window("m"),
                {**_window("m"), "reference_series": "polybolt"},
            ],
            "reference_series_policy": {"choices": {"m": "rtds"}, "provenance": "ASSUMED"},
        },
        root=root,
    )
    assert chosen["result"]["included"][0]["reference_series"] == "rtds"
    mixed = call(
        "selection.resolve",
        {
            "windows": [
                _window("m"),
                {**_window("m"), "source_kind": "trading_capture", "capture_id": "run-9"},
            ]
        },
        root=root,
    )
    assert mixed["result"]["excluded"][0]["reason"] == "capture_provenance_required"
    kept = call(
        "selection.resolve",
        {
            "windows": [
                _window("m"),
                {**_window("m"), "source_kind": "trading_capture", "capture_id": "run-9"},
            ],
            "capture_choices": {"m": "collection_recording"},
        },
        root=root,
    )
    kept_row = kept["result"]["included"][0]
    assert kept_row.get("source_kind", "collection_recording") == "collection_recording"
    assert "capture_id" not in kept_row


def test_preview_omits_unmeasured_duration_and_refuses_a_large_grid(tmp_path: Path) -> None:
    root = tmp_path / "wf"
    saved = call(
        "experiment.save",
        {"windows": [_window()], "parameters": {}, "scenarios": ["favorable"]},
        root=root,
    )
    preview = call(
        "experiment.preview", {"experiment_id": saved["result"]["experiment_id"]}, root=root
    )
    assert preview["result"]["estimate"] is None
    assert preview["result"]["estimate_reason"] == "no_measured_sample"
    refused = call(
        "experiment.resolve",
        {
            "windows": [_window(f"m{i}") for i in range(4)],
            "parameters": {},
            "grid": {"axes": {"entry.min_edge_per_share": ["0.01", "0.02"]}},
            "max_units": 4,
        },
        root=root,
    )
    assert refused["error"]["code"] == "resource_limit"
    assert refused["error"]["fields"]["planned"] == 8


def test_reads_do_not_touch_files_and_traces_report_availability(tmp_path: Path) -> None:
    root = tmp_path / "wf"
    saved = call(
        "experiment.save",
        {
            "windows": [_window()],
            "parameters": {},
            "units": [{"id": "u1", "kind": "digest", "value": 1, "market_id": "m1"}],
        },
        root=root,
    )
    started = call(
        "job.start",
        {"experiment_id": saved["result"]["experiment_id"], "job_id": "job-read"},
        root=root,
    )
    assert started["ok"] is True
    _wait(root, "job-read", lambda item: item["result"]["state"] == "completed")
    before = _snapshot(root)
    opened = call("results.open", {"job_id": "job-read"}, root=root)
    compared = call(
        "results.compare",
        {"left_job_id": "job-read", "right_job_id": "job-read", "pairing": "common_markets"},
        root=root,
    )
    traced = call("results.trace", {"job_id": "job-read", "unit_id": "u1"}, root=root)
    assert opened["ok"] and compared["ok"]
    assert traced["result"]["available"] is False
    assert _snapshot(root) == before
    call(
        "comparison.save",
        {"left_job_id": "job-read", "right_job_id": "job-read", "pairing": "common_markets"},
        root=root,
    )
    assert _snapshot(root) != before


def test_partial_evaluation_stays_visible_to_the_guard(tmp_path: Path) -> None:
    root = tmp_path / "wf"
    research = tmp_path / "research"
    split = _split(tmp_path)
    request = {
        "purpose": "evaluation",
        "market_ids": ["hold-market"],
        "research_root": str(research),
        "split_file": str(split),
    }
    first = call("evaluation.open", request, root=root)
    assert first["ok"] is True
    assert not any(path.name == "summaries.parquet" for path in research.rglob("*"))
    second = call("evaluation.open", request, root=root)
    assert second["error"]["code"] == "holdout_exposed"
    lock = StateDirectoryLock(research / "guard", filename=".workflow_job.lock")
    lock.acquire()
    try:
        busy = call(
            "evaluation.open",
            {**request, "market_ids": ["dev-market"]},
            root=root,
        )
    finally:
        lock.release()
    assert busy["error"]["code"] == "guard_busy"


def test_cli_and_notebook_share_the_replay_result(tmp_path: Path) -> None:
    root = tmp_path / "wf"
    direct = run_closed_loop_fixture({"id": "direct", "market_id": "m1"})
    saved = call(
        "experiment.save",
        {
            "windows": [_window()],
            "parameters": {},
            "units": [{"id": "replay", "kind": "replay_engine", "market_id": "m1"}],
        },
        root=root,
    )
    experiment_id = saved["result"]["experiment_id"]
    request = {"windows": [_window()], "parameters": {}}
    via_call = call("experiment.resolve", request, root=root)
    via_notebook = resolve_experiment(request, root=root)
    assert via_notebook["result"]["configurations"] == via_call["result"]["configurations"]
    started = call("job.start", {"experiment_id": experiment_id, "job_id": "replay-job"}, root=root)
    assert started["ok"] is True
    _wait(root, "replay-job", lambda item: item["result"]["state"] == "completed")
    envelope, frame = open_results({"job_id": "replay-job"}, root=root)
    row = envelope["result"]["windows"][0]
    assert row["fill_price"] == direct["fill_price"] == "0.55"
    assert row["fill_at_ns"] == direct["fill_at_ns"]
    assert frame.iloc[0]["fill_price"] == direct["fill_price"]
    assert row["realized_pnl"] is None
    cli_request = tmp_path / "request.json"
    cli_request.write_text(json.dumps({"job_id": "replay-job"}), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tyrex_pm.research.cli",
            "workflow",
            "results.open",
            "--request",
            str(cli_request),
            "--root",
            str(root),
        ],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["result"]["windows"][0]["fill_price"] == direct["fill_price"]
    assert payload["result"]["windows"][0]["fill_at_ns"] == direct["fill_at_ns"]
    assert completed.stderr == ""


def test_profile_translation_and_ask70_limits(tmp_path: Path) -> None:
    root = tmp_path / "wf"
    generated = tmp_path / "generated"
    settlement = call(
        "candidate.save",
        {
            "candidate_id": "settle1",
            "strategy_kind": "settlement_value",
            "parameters": {
                "entry": {"tau_min_s": 30, "tau_max_s": 240, "min_edge_per_share": "0.02"},
                "exit": {"policy": "hold_to_resolution"},
            },
            "rationale": "fixture",
            "result_ids": ["replay-job"],
        },
        root=root,
    )
    assert settlement["ok"] is True
    operational = _paper_operational(tmp_path / "paper-state", settlement=True)
    refused = call(
        "profile.generate",
        {
            "candidate_id": "settle1",
            "operational": {**operational, "research_defaults": {"quality": {}}},
            "output_directory": str(generated),
        },
        root=root,
    )
    assert refused["error"]["code"] == "research_defaults_refused"
    created = call(
        "profile.generate",
        {"candidate_id": "settle1", "operational": operational, "output_directory": str(generated)},
        root=root,
    )
    assert created["ok"] is True, created
    text = Path(created["result"]["path"]).read_text(encoding="utf-8")
    assert "min_edge_per_share" in text
    assert "PROVISIONAL" not in text
    validated = call("profile.validate", {"path": created["result"]["path"]}, root=root)
    assert validated["result"]["schema_valid"] is True
    assert validated["result"]["qualified"] is False
    ask = call(
        "candidate.save",
        {
            "candidate_id": "ask1",
            "strategy_kind": "ask70",
            "parameters": {
                "entry_ask_threshold": "0.70",
                "leg_preference": "first_hit",
                "max_price_pad": "0.08",
                "tau_min_s": 105.0,
                "tau_max_s": 290.0,
                "max_clock_uncertainty_ms": 750.0,
            },
            "rationale": "recording fixture",
        },
        root=root,
    )
    assert ask["ok"] is True
    ask_ops = _paper_operational(tmp_path / "ask-paper", settlement=False)
    ask_ops["lifecycle"]["mode"] = "flatten_before_close"
    ask_ops["market"]["run_mode"] = "continuous"
    blocked = call(
        "profile.generate",
        {"candidate_id": "ask1", "operational": ask_ops, "output_directory": str(generated)},
        root=root,
    )
    assert blocked["error"]["code"] == "ask70_lifecycle"
    ask_ops["market"]["run_mode"] = "single_window"
    ask_ops["protection"] = {
        "mark": "bid",
        "size_mode": "full",
        "take_profit": {"style": "resting_gtd", "absolute": "0.85"},
        "stop_loss": {"style": "stop_market_fak", "absolute": "0.55"},
        "trailing": {"enabled": False, "activation": "immediate", "mark": "bid"},
    }
    ask_profile = call(
        "profile.generate",
        {
            "candidate_id": "ask1",
            "operational": ask_ops,
            "output_directory": str(generated),
            "run_name": "ask70_workflow_paper",
        },
        root=root,
    )
    assert ask_profile["ok"] is True, ask_profile
    preflight = call("paper.preflight", {"profile": ask_profile["result"]["path"]}, root=root)
    before = _snapshot(tmp_path)
    again = call("paper.preflight", {"profile": ask_profile["result"]["path"]}, root=root)
    assert again["result"]["launch_allowed"] is False
    assert any(item["code"] == "step2_follow_up_open" for item in again["result"]["blockers"])
    assert again["result"]["mutates_account"] is False
    assert _snapshot(tmp_path) == before
    assert preflight["result"]["s0_observations"][2]["role"] == "replacement"
    assert preflight["result"]["s0_observations"][2]["accepted"] is False


def test_collection_stop_names_the_session(tmp_path: Path) -> None:
    assert collector_stop_matches(
        {"session_id": "s1", "generation": "2"}, session_id="s1", generation="2"
    )
    assert not collector_stop_matches(
        {"session_id": "s1", "generation": "1"}, session_id="s1", generation="2"
    )
    root = tmp_path / "wf"
    warehouse = tmp_path / "warehouse"
    record = {
        "collection": "c1",
        "warehouse": str(warehouse),
        "generation": "2",
        "session_id": "sess-2",
    }
    (root / "collections").mkdir(parents=True)
    (root / "collections" / "c1.json").write_text(canonical(record), encoding="utf-8")
    wrong = call(
        "collection.stop",
        {"collection": "c1", "session_id": "sess-2", "generation": "1"},
        root=root,
    )
    assert wrong["error"]["code"] == "stop_target"
    stopped = call(
        "collection.stop",
        {"collection": "c1", "session_id": "sess-2", "generation": "2"},
        root=root,
    )
    assert stopped["ok"] is True
    payload = json.loads(Path(stopped["result"]["path"]).read_text(encoding="utf-8"))
    assert payload["session_id"] == "sess-2"
    assert payload["generation"] == "2"


def _paper_operational(state: Path, *, settlement: bool) -> dict:
    state.mkdir(parents=True, exist_ok=True)
    body = {
        "venue": "paper",
        "paper": {
            "initial_collateral": "100",
            "order_latency_ms": 300,
            "participation": "1",
            "resolution_source": "chain_read",
        },
        "state_directory": str(state / "paper-execution"),
        "report_directory": str(state / "paper-reports"),
        "market": {
            "family": "btc_updown_5m",
            "binance_symbol": "BTCUSDT",
            "require_ssr_price_match": False,
            "run_mode": "single_window",
            "preparation_lead_s": 45,
            "evaluation_interval_s": 1,
            "maximum_book_age_ms": 2000,
            "maximum_candidate_age_ms": 5000,
            "maximum_duration_s": 300,
        },
        "account": {
            "refresh_interval_s": 5,
            "snapshot_max_age_s": 15,
            "read_timeout_s": 4,
            "retry_attempts": 3,
            "retry_base_delay_s": 0.25,
        },
        "risk": {
            "target_notional": "5",
            "maximum_total_debit": "5",
            "fee_reserve_rate": "0.02",
        },
        "lifecycle": {
            "mode": "hold_to_resolution",
            "mandatory_exit_before_end_s": 20,
            "manual_deadline_before_end_s": 5,
            "evidence_timeout_s": 15,
            "reconciliation_interval_s": 0.25,
            "exit_retry_limit": 3,
            "exit_retry_budget_s": 10,
            "minimum_exit_price": "0.01",
            "sell_size_increment": "0.01",
            "dust_abs_shares": "0.01",
            "dust_max_notional": "0.05",
            "prepare_budget_ms": 2000,
            "refresh_wait_budget_ms": 500,
        },
    }
    if settlement:
        body["settlement"] = {
            "ledger_path": str(state / "paper-ledger.jsonl"),
            "hold_commit_budget_s": 10,
            "redemption_mode": "operator",
            "resolution_poll_interval_s": 30,
            "trial": {
                "max_debit_per_episode": "5",
                "max_episodes": 10,
                "max_unresolved_count": 6,
                "max_unresolved_cost": "30",
                "trial_loss_budget": "60",
            },
        }
        body["market"]["run_mode"] = "single_window"
    return body


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    found = {}
    for path in root.rglob("*"):
        if path.is_file() and path.name != "worker.log":
            stat = path.stat()
            found[str(path)] = (stat.st_mtime_ns, stat.st_size)
    return found


def _wait(root: Path, job_id: str, predicate, timeout: float = 20) -> dict:
    import time

    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = call("job.status", {"job_id": job_id}, root=root)
        if last["ok"] and predicate(last):
            return last
        time.sleep(0.05)
    pytest.fail(f"job did not reach the expected state: {last}")
