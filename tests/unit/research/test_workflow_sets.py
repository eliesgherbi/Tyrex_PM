"""Set lineage and dependents, labels, export, holdout reservations and trash sizes."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from tyrex_pm.research.workflows import inventory
from tyrex_pm.research.workflows.api import call
from tyrex_pm.research.workflows.artifacts import write_json


def _set(root: Path, selection_id: str, parent: str | None = None, version: int = 1) -> None:
    body = {
        "selection_id": selection_id,
        "name": f"Set {selection_id}",
        "digest": "d",
        "research_policy": "replay_research_v2_1",
        "included": [
            {
                "market_id": "m1",
                "window_start_ns": 1_790_000_000_000_000_000,
                "window_end_ns": 1_790_000_300_000_000_000,
                "outcome": "up",
                "book_profile": {"valid_ratio": 0.95, "longest_hole_s": 12.0},
            },
            {"market_id": "m2", "outcome": "down"},
        ],
    }
    if parent:
        body.update(parent_selection_id=parent, version=version)
    write_json(root / "selections" / f"{selection_id}.json", body)


def test_lineage_lists_versions_runs_and_candidates(tmp_path: Path) -> None:
    _set(tmp_path, "v1")
    _set(tmp_path, "v2", parent="v1", version=2)
    write_json(
        tmp_path / "experiments" / "e1.json",
        {"selection_id": "v1", "scenario_name": "friction_v3", "configurations": [{"name": "A"}]},
    )
    write_json(tmp_path / "replay_batches" / "b.json", {"name": "Batch", "experiments": ["e1"]})
    write_json(tmp_path / "jobs" / "j1" / "job.json", {"job_id": "j1", "experiment_id": "e1"})
    write_json(tmp_path / "jobs" / "j1" / "status.json", {"state": "completed"})
    write_json(
        tmp_path / "candidates" / "c1.json",
        {"candidate_id": "c1", "name": "Keep A", "result_ids": ["j1"]},
    )
    first = call("selection.lineage", {"selection_id": "v1"}, root=tmp_path)["result"]
    assert [child["selection_id"] for child in first["children"]] == ["v2"]
    assert first["runs"][0]["job_id"] == "j1" and first["runs"][0]["name"] == "Batch"
    assert first["candidates"][0]["name"] == "Keep A"
    second = call("selection.lineage", {"selection_id": "v2"}, root=tmp_path)["result"]
    assert second["ancestors"][0]["selection_id"] == "v1"
    assert second["runs"] == []


def test_renaming_keeps_windows_and_logs_the_change(tmp_path: Path) -> None:
    _set(tmp_path, "s1")
    out = call(
        "selection.annotate",
        {"selection_id": "s1", "name": "Afternoon", "description": "6 Oct, calm"},
        root=tmp_path,
    )
    assert out["ok"], out
    saved = json.loads((tmp_path / "selections" / "s1.json").read_text())
    assert saved["name"] == "Afternoon" and saved["digest"] == "d"
    assert saved["label_history"][0]["before"]["name"] == "Set s1"
    assert (
        call("selection.annotate", {"selection_id": "s1", "name": " "}, root=tmp_path)["error"][
            "code"
        ]
        == "invalid_name"
    )


def test_export_writes_csv_and_json_membership(tmp_path: Path) -> None:
    _set(tmp_path, "s1")
    out = call("selection.export", {"selection_id": "s1", "format": "csv"}, root=tmp_path)["result"]
    rows = list(csv.DictReader(io.StringIO(out["content"])))
    assert [row["market_id"] for row in rows] == ["m1", "m2"]
    assert rows[0]["longest_hole_s"] == "12.0"
    assert Path(out["path"]).is_file()
    body = json.loads(
        call("selection.export", {"selection_id": "s1", "format": "json"}, root=tmp_path)["result"][
            "content"
        ]
    )
    assert body["windows"] == 2 and body["research_policy"] == "replay_research_v2_1"


def test_a_reservation_warns_replay_previews(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        inventory,
        "discover",
        lambda scope: {"windows": [{"market_id": "m2", "block_id": "c:s"}]},
    )
    saved = call("reservation.save", {"name": "Tune holdout", "block_ids": ["c:s"]}, root=tmp_path)
    assert saved["ok"] and saved["result"]["windows"] == 1
    from tyrex_pm.research.workflows.sets import reserved_overlap

    assert reserved_overlap(tmp_path, {"m1", "m2"})[0]["windows"] == 1
    listed = call("reservation.list", {}, root=tmp_path)["result"]["reservations"]
    assert listed[0]["name"] == "Tune holdout"
    call("reservation.delete", {"reservation_id": listed[0]["reservation_id"]}, root=tmp_path)
    assert reserved_overlap(tmp_path, {"m2"}) == []


def test_trash_reports_size_and_age_and_purges_by_age(tmp_path: Path) -> None:
    _set(tmp_path, "s1")
    call("selection.delete", {"selection_id": "s1"}, root=tmp_path)
    entry = call("trash.list", {}, root=tmp_path)["result"]["entries"][0]
    assert entry["size_bytes"] > 0 and entry["age_days"] == 0.0
    kept = call(
        "trash.purge", {"confirm": "delete permanently", "older_than_days": 30}, root=tmp_path
    )
    assert kept["result"]["purged"] == 0
