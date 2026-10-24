"""Recording from the workspace: preflight, one recording per warehouse, status and stop."""

from __future__ import annotations

import json
import time
from pathlib import Path

from tyrex_pm.research.workflows import recording
from tyrex_pm.research.workflows.api import call


class _Process:
    pid = 4242


def _ready(monkeypatch, tmp_path: Path, reachable: bool = True) -> Path:
    warehouse = tmp_path / "warehouse_rec_test"
    (warehouse / "collections").mkdir(parents=True)
    monkeypatch.setattr(recording, "warehouse_roots", lambda: [warehouse.resolve()])
    monkeypatch.setattr(recording, "_credentials_present", lambda: True)
    monkeypatch.setattr(
        recording,
        "reachability",
        lambda timeout_s=6.0: [
            {"name": name, "url": "", "ok": reachable, "reason": "" if reachable else "TLS"}
            for name in recording.PROBES
        ],
    )
    monkeypatch.setattr(recording.subprocess, "Popen", lambda *a, **k: _Process())
    return warehouse


def test_start_is_refused_when_polymarket_is_unreachable(tmp_path: Path, monkeypatch) -> None:
    warehouse = _ready(monkeypatch, tmp_path, reachable=False)
    refused = call(
        "recording.start", {"warehouse": str(warehouse), "duration_s": 600}, root=tmp_path / "wf"
    )
    assert refused["error"]["code"] == "venue_unreachable"


def test_duration_is_bounded(tmp_path: Path, monkeypatch) -> None:
    warehouse = _ready(monkeypatch, tmp_path)
    for seconds in (60, 25 * 3600):
        refused = call(
            "recording.start",
            {"warehouse": str(warehouse), "duration_s": seconds},
            root=tmp_path / "wf",
        )
        assert refused["error"]["code"] == "invalid_duration"


def test_a_recording_reports_its_session_and_stops_cleanly(tmp_path: Path, monkeypatch) -> None:
    warehouse = _ready(monkeypatch, tmp_path)
    root = tmp_path / "wf"
    started = call(
        "recording.start",
        {"warehouse": str(warehouse), "duration_s": 600, "name": "Check"},
        root=root,
    )
    assert started["ok"], started
    recording_id = started["result"]["recording_id"]
    assert (
        call("recording.status", {"recording_id": recording_id}, root=root)["result"]["state"]
        == "starting"
    )
    # The collector prints its identities first, then heartbeats into status.json.
    log = root / "recordings" / f"{recording_id}.log"
    log.write_text(json.dumps({"collection_id": "c1", "session_id": "s1", "root": "x"}) + "\n")
    status = warehouse / "collections" / "c1" / "status.json"
    status.parent.mkdir(parents=True)
    status.write_text(
        json.dumps({"heartbeat_at": time.time_ns(), "subscribed_tokens": 8, "feeds": {}})
    )
    view = call("recording.status", {"recording_id": recording_id}, root=root)["result"]
    assert view["state"] == "recording"
    assert view["collection"] == "c1"
    assert view["host_status"]["subscribed_tokens"] == 8
    second = call("recording.start", {"warehouse": str(warehouse), "duration_s": 600}, root=root)
    assert second["error"]["code"] == "recording_running"
    stopped = call("recording.stop", {"recording_id": recording_id}, root=root)
    assert stopped["ok"], stopped
    stop_file = warehouse / "collections" / "c1" / "sessions" / "s1.collector-stop"
    assert json.loads(stop_file.read_text())["session_id"] == "s1"


def _capture_argv(monkeypatch) -> list:
    seen: list = []

    def popen(argv, **_kwargs):
        seen.append(argv)
        return _Process()

    monkeypatch.setattr(recording.subprocess, "Popen", popen)
    return seen


def test_a_recording_finishes_its_last_window_and_waits_for_winners(
    tmp_path: Path, monkeypatch
) -> None:
    warehouse = _ready(monkeypatch, tmp_path)
    seen = _capture_argv(monkeypatch)
    started = call(
        "recording.start", {"warehouse": str(warehouse), "duration_s": 1200}, root=tmp_path / "wf"
    )
    assert started["ok"], started
    argv = seen[-1]
    assert "--finish-windows" in argv
    assert argv[argv.index("--outcome-wait") + 1] == f"{recording.OUTCOME_WAIT_DEFAULT_S}s"
    plan = call("recording.plan", {}, root=tmp_path / "wf")["result"]
    assert plan["closing"]["finish_windows_default"] is True


def test_closing_can_be_turned_off_and_the_wait_is_bounded(tmp_path: Path, monkeypatch) -> None:
    warehouse = _ready(monkeypatch, tmp_path)
    seen = _capture_argv(monkeypatch)
    request = {"warehouse": str(warehouse), "duration_s": 600}
    off = call(
        "recording.start",
        {**request, "finish_windows": False, "outcome_wait_s": 0},
        root=tmp_path / "wf",
    )
    assert off["ok"] and "--finish-windows" not in seen[-1] and "--outcome-wait" not in seen[-1]
    too_long = call(
        "recording.start", {**request, "outcome_wait_s": 2 * 3600}, root=tmp_path / "wf2"
    )
    assert too_long["error"]["code"] == "invalid_outcome_wait"


def _session(warehouse: Path, collection: str, name: str, body: dict) -> None:
    folder = warehouse / "collections" / collection / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(json.dumps({"session_id": name, **body}))


def test_fetching_winners_resumes_the_collection_for_outcomes_only(
    tmp_path: Path, monkeypatch
) -> None:
    warehouse = _ready(monkeypatch, tmp_path)
    _session(warehouse, "c1", "s1", {"started_at": 10, "stopped_at": 50, "windows_until": 40})
    seen = _capture_argv(monkeypatch)
    root = tmp_path / "wf"
    fetched = call("recording.fetch_outcomes", {"block_id": "c1:s1"}, root=root)
    assert fetched["ok"], fetched
    argv = seen[-1]
    assert argv[argv.index("--resume") + 1] == "c1"
    assert "--outcomes-only" in argv and "--config" not in argv
    recording_id = fetched["result"]["recording_id"]
    record = json.loads((root / "recordings" / f"{recording_id}.json").read_text())
    assert record["kind"] == "outcome_fetch" and record["block_id"] == "c1:s1"
    unknown = call("recording.fetch_outcomes", {"block_id": "c1:nope"}, root=root)
    assert unknown["error"]["code"] == "not_found"


def test_capture_blocks_end_where_complete_windows_end(tmp_path: Path) -> None:
    from tyrex_pm.research.workflows.inventory import _assign_blocks, capture_blocks

    warehouse = tmp_path / "w"
    _session(warehouse, "c1", "rec", {"started_at": 100, "stopped_at": 900, "windows_until": 400})
    _session(warehouse, "c1", "fetch", {"started_at": 950, "stopped_at": 990, "windows_until": 950})
    _session(warehouse, "c1", "old", {"started_at": 1000, "stopped_at": 1350})
    blocks = capture_blocks([warehouse])
    assert sorted(block["session_id"] for block in blocks) == ["old", "rec"]  # fetch has none
    rows = [
        {"warehouse": str(warehouse), "collection": "c1", "window_start_ns": start,
         "window_end_ns": start + 300}
        for start in (100, 400, 1000, 1100)
    ]
    _assign_blocks(rows, blocks)
    assert [row["block_id"] for row in rows] == ["c1:rec", None, "c1:old", "c1:old"]
    # An older session that stopped inside a window: the window is marked as cut.
    assert [row["recording_cut_at_ns"] for row in rows] == [None, None, None, 1350]


def test_a_cut_window_is_named_but_never_newly_excluded(monkeypatch) -> None:
    from tyrex_pm.research.workflows import recipes

    cut = {"recording_cut_at_ns": 5}
    monkeypatch.setattr(recipes, "_data_reason", lambda row, req, recipe: "book_valid_below_90")
    named = recipes._requirement_reason(cut, {}, "eligible_windows")
    assert named == "recording_ended_before_close"
    assert recipes._requirement_reason({}, {}, "eligible_windows") == "book_valid_below_90"
    monkeypatch.setattr(recipes, "_data_reason", lambda row, req, recipe: None)
    assert recipes._requirement_reason(cut, {}, "eligible_windows") is None
