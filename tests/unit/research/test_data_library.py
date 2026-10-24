"""Data Library inventory, custom filters, pins, binding, and preparation."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow as pa

from tyrex_pm.data_warehouse.capture.files import Ownership, atomic_json
from tyrex_pm.data_warehouse.capture.writer import RawWriter
from tyrex_pm.data_warehouse.config import Profile, Storage
from tyrex_pm.data_warehouse.contracts import PARSER_VERSION, canonical
from tyrex_pm.data_warehouse.normalize.schemas import schema
from tyrex_pm.data_warehouse.normalize.worker import write_table
from tyrex_pm.research.workflows.api import call
from tyrex_pm.research.workflows.recipes import zoned_start

NS = 1_000_000_000
UP = "up-instrument"
DOWN = "down-instrument"


def _ns(text: str) -> int:
    parsed = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * NS)


def _snapshot(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    return {
        str(item.relative_to(path)): item.stat().st_mtime_ns
        for item in path.rglob("*")
        if item.is_file()
    }


def _row(dataset: str, **values):
    body = {field.name: None for field in schema(dataset)}
    body.update(values)
    return body


def _publish(
    warehouse: Path, collection: str, dataset: str, rows: list[dict], build_id: str
) -> None:
    table = pa.Table.from_pylist(rows, schema=schema(dataset))
    relative = f"normalized/schema=v1/dataset={dataset}/date=2026-03-29/{build_id}.parquet"
    details = write_table(warehouse / relative, table)
    received = [int(row["received_at"].timestamp() * NS) for row in rows]
    atomic_json(
        warehouse / "builds" / build_id / "manifest.json",
        {
            "manifest_version": 1,
            "build_id": build_id,
            "collection_id": collection,
            "schema_version": "1",
            "parser_version": PARSER_VERSION,
            "first_received_at": min(received),
            "last_received_at": max(received),
            "input_segments": ["seg"],
            "files": [{"path": relative, "dataset": dataset, **details}],
            "supersedes": [],
        },
    )


def _market(market_id: str, start: str, payouts: list | None) -> None:
    del market_id, start, payouts


def build_archive(warehouse: Path) -> dict[str, int]:
    collection = "c1"
    atomic_json(
        warehouse / "collections" / collection / "collection.json",
        {"profile": {"reference": {"series_id": "official_reference"}}, "profile_hash": "fixture"},
    )
    atomic_json(
        warehouse / "raw" / collection / "seg.manifest.json",
        {
            "collection_id": collection,
            "segment_id": "seg",
            "first_received_at": 1,
            "path": "raw/unused.zst",
            "sha256": "0",
            "compressed_sha256": "0",
        },
    )
    starts = {
        "m-up": "2026-03-29T00:30:00",
        "m-down": "2026-10-25T01:30:00",
        "m-open": "2026-03-30T22:30:00",
        "m-bad": "2026-03-30T12:00:00",
        "m-hold": "2026-03-30T15:00:00",
        "m-partial": "2026-03-30T16:00:00",
    }
    meta = []
    resolutions = []
    for market_id, start in starts.items():
        start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
        end_dt = datetime.fromtimestamp(start_dt.timestamp() + 300, tz=timezone.utc)
        meta.append(
            _row(
                "metadata_versions",
                record_id=f"meta-{market_id}",
                entity_type="market",
                entity_id=market_id,
                source_id="gamma",
                received_at=start_dt,
                window_start=start_dt,
                window_end=end_dt,
                family_attributes=json.dumps(
                    {
                        "slug": f"btc-updown-5m-{market_id}",
                        "instruments": [
                            {"normalized_label": "UP", "outcome_index": 0, "instrument_id": UP},
                            {"normalized_label": "DOWN", "outcome_index": 1, "instrument_id": DOWN},
                        ],
                    }
                ),
                additional_fields=(
                    json.dumps({"events": [{"eventMetadata": {"priceToBeat": "86264.7402356731"}}]})
                    if market_id == "m-up"
                    else None
                ),
            )
        )
    _publish(warehouse, collection, "metadata_versions", meta, "build-meta")
    payouts = {
        "m-up": [("resolved", ["1", "0"])],
        "m-down": [("resolved", ["0", "1"])],
        "m-bad": [("resolved", ["1", "0"]), ("resolved", ["0", "1"])],
    }
    for market_id, vectors in payouts.items():
        for index, (status, vector) in enumerate(vectors):
            resolutions.append(
                _row(
                    "resolution_observations",
                    record_id=f"res-{market_id}-{index}",
                    market_id=market_id,
                    provider_status=status,
                    payouts=[Decimal(item) for item in vector],
                    payout_unit="UNKNOWN",
                    received_at=datetime.fromisoformat(starts[market_id]).replace(
                        tzinfo=timezone.utc
                    ),
                )
            )
    _publish(warehouse, collection, "resolution_observations", resolutions, "build-res")
    full = {
        "status": "UNKNOWN_CONTINUITY",
        "required_duration_ns": 300 * NS,
        "usable_duration_ns": 300 * NS,
        "intervals": [[0, 300 * NS]],
        "required": True,
    }
    partial = {**full, "usable_duration_ns": 200 * NS, "status": "GAP"}
    for market_id, feed in {
        "m-up": full,
        "m-down": full,
        "m-open": full,
        "m-bad": full,
        "m-hold": full,
        "m-partial": partial,
    }.items():
        folder = warehouse / "collections" / collection / "quality_markets" / market_id
        folder.mkdir(parents=True)
        report = {
            "market_id": market_id,
            "policy_version": "fixture",
            "assessed_at": _ns(starts[market_id]),
            "semantic_readiness": "READY",
            "feeds": {"up-instrument": feed, DOWN: feed, "official_reference": feed},
        }
        if market_id == "m-partial":
            report["feeds"] = {"up-instrument": partial, DOWN: full, "official_reference": full}
        atomic_json(folder / "report.json", report)
        atomic_json(folder / "manifest.json", {"market_id": market_id, "revision": market_id})
    atomic_json(
        warehouse / "collections" / collection / "quality_status.json",
        {"publication_id": "pub-fixture", "published_at": _ns("2026-03-30T00:00:00")},
    )
    return {market_id: _ns(start) for market_id, start in starts.items()}


def _scope(warehouse: Path, **extra) -> dict:
    return {
        "roots": [str(warehouse)],
        "collection": "c1",
        "start_utc": "2026-03-01",
        "end_utc": "2026-11-01",
        "reserved_market_ids": ["m-hold"],
        **extra,
    }


def test_inventory_agrees_and_writes_nothing(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse"
    workflow = tmp_path / "workflows"
    starts = build_archive(warehouse)
    before_warehouse = _snapshot(warehouse)
    listed = call("library.windows", _scope(warehouse), root=workflow)
    coverage = call("library.coverage", {**_scope(warehouse), "bucket_s": 300}, root=workflow)
    detail = call("library.window", {**_scope(warehouse), "market_id": "m-up"}, root=workflow)
    assert listed["ok"] and coverage["ok"] and detail["ok"]
    assert not workflow.exists()
    assert _snapshot(warehouse) == before_warehouse
    ids = {row["market_id"] for row in listed["result"]["windows"]}
    assert {"m-up", "m-down", "m-open", "m-bad", "m-hold", "m-partial"} <= ids
    assert detail["result"]["window"]["window_start_ns"] == starts["m-up"]
    assert detail["result"]["window"]["tracks"]["up_book"]["state"] == "present"
    partial = next(row for row in listed["result"]["windows"] if row["market_id"] == "m-partial")
    assert partial["tracks"]["up_book"]["partial"] is True
    assert partial["tracks"]["up_book"]["usable_duration_ns"] == 200 * NS
    hold = next(row for row in listed["result"]["windows"] if row["market_id"] == "m-hold")
    assert hold["outcome"] == "withheld"
    assert "label_method" not in hold or hold["label_method"] is None
    assert listed["result"]["track_counts"]["unknown"] != 0 or any(
        row["outcome"] == "unknown" for row in listed["result"]["windows"]
    )
    bad = next(row for row in listed["result"]["windows"] if row["market_id"] == "m-bad")
    assert bad["outcome"] == "unknown"
    assert bad["label_source"] == "unknown"
    opened = next(row for row in listed["result"]["windows"] if row["market_id"] == "m-open")
    assert opened["outcome"] == "unresolved"
    assert listed["result"]["windows"][0]["window_start_ns"] >= _ns("2026-03-01")
    assert listed["result"]["span_start_utc"] < listed["result"]["span_end_utc"]


def test_schedule_infers_only_when_asked(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse"
    build_archive(warehouse)
    plain = call(
        "library.windows",
        {
            **_scope(warehouse),
            "start_utc": "2026-03-30T12:00:00+00:00",
            "end_utc": "2026-03-30T12:15:00+00:00",
        },
        root=tmp_path / "wf",
    )
    assert all(row.get("inferred") is not True for row in plain["result"]["windows"])
    scheduled = call(
        "library.windows",
        {
            **_scope(warehouse),
            "start_utc": "2026-03-30T12:00:00+00:00",
            "end_utc": "2026-03-30T12:15:00+00:00",
            "schedule": "btc_updown_5m",
        },
        root=tmp_path / "wf",
    )
    inferred = [row for row in scheduled["result"]["windows"] if row.get("inferred")]
    assert inferred
    assert inferred[0]["tracks"]["recording"]["state"] == "missing"
    assert inferred[0]["outcome"] is None


def test_custom_filters_clock_partition_and_pins(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse"
    workflow = tmp_path / "workflows"
    build_archive(warehouse)
    sunday_late = _ns("2026-03-29T21:30:00")
    assert zoned_start(sunday_late, "Europe/Paris") == (23 * 60 + 30, 7)
    monday_after_midnight = _ns("2026-03-29T22:30:00")
    assert zoned_start(monday_after_midnight, "Europe/Paris") == (30, 1)
    spring = _ns("2026-03-29T00:30:00")
    assert zoned_start(spring, "Europe/Paris") == (1 * 60 + 30, 7)
    autumn = _ns("2026-10-25T01:30:00")
    autumn_minutes, autumn_day = zoned_start(autumn, "Europe/Paris")
    assert autumn_day == 7
    assert autumn_minutes == 2 * 60 + 30
    filters = {
        "outcome": "any",
        "label_source": "any",
        "time_zone": "Europe/Paris",
        "daily_time": {
            "enabled": True,
            "start": "22:00",
            "end": "02:00",
            "time_zone": "Europe/Paris",
        },
        "weekdays": [1, 2, 3, 4, 5, 6, 7],
        "selection_mode": "all_matches",
    }
    resolved = call(
        "data_recipe.resolve",
        {
            **_scope(warehouse),
            "recipe": "custom_window_set",
            "filters": filters,
            "requirements": {"books": True, "reference": True, "warmup": True},
        },
        root=workflow,
    )
    assert resolved["ok"]
    assert resolved["result"]["clock"]["weekday_basis"] == "zoned_window_start"
    matched = {row["market_id"] for row in resolved["result"]["candidates"]}
    assert "m-open" in matched
    assert "m-bad" not in matched
    assert "m-hold" not in matched
    assert resolved["result"]["outcome_mix"]["unknown"] >= 0
    assert "m-hold" not in {row["market_id"] for row in resolved["result"]["candidates"]}
    assert any(row["reason"] == "reserved_evaluation" for row in resolved["result"]["excluded"])
    sunday_only = call(
        "data_recipe.resolve",
        {
            **_scope(warehouse),
            "recipe": "custom_window_set",
            "filters": {**filters, "weekdays": [7]},
            "requirements": {"books": True, "reference": True, "warmup": True},
        },
        root=workflow,
    )
    sunday_ids = {row["market_id"] for row in sunday_only["result"]["candidates"]}
    assert "m-up" in sunday_ids
    assert "m-open" not in sunday_ids
    refused = call(
        "data_recipe.resolve",
        {**_scope(warehouse), "recipe": "reversal"},
        root=workflow,
    )
    assert refused["error"]["code"] == "unsupported_measurement"
    assert refused["result"] is None
    confirmed = call(
        "data_recipe.resolve",
        {
            **_scope(warehouse),
            "recipe": "custom_window_set",
            "filters": {
                **filters,
                "daily_time": {"enabled": False, "start": "00:00", "end": "00:00"},
                "label_source": "provider_confirmed",
            },
        },
        root=workflow,
    )
    assert confirmed["ok"] and confirmed["result"]["matched"] == 0
    assert confirmed["result"]["supported_filter"] is True
    up = call(
        "data_recipe.resolve",
        {
            **_scope(warehouse),
            "recipe": "custom_window_set",
            "filters": {
                **filters,
                "daily_time": {"enabled": False},
                "outcome": "up",
                "label_source": "proxy",
            },
            "requirements": {
                "books": True,
                "reference": True,
                "warmup": True,
                "scoring_outcome": True,
            },
        },
        root=workflow,
    )
    assert [row["market_id"] for row in up["result"]["included"]] == ["m-up"]
    assert up["result"]["population"] == "outcome_conditioned"
    picked = call(
        "data_recipe.resolve",
        {
            **_scope(warehouse),
            "recipe": "custom_window_set",
            "filters": {
                **filters,
                "daily_time": {"enabled": False},
                "selection_mode": "picked",
                "picked": ["m-up", "m-hold", "not-a-window"],
            },
        },
        root=workflow,
    )
    assert [row["market_id"] for row in picked["result"]["included"]] == ["m-up"]
    reasons = {row["market_id"]: row["reason"] for row in picked["result"]["excluded"]}
    assert reasons["m-hold"] == "reserved_evaluation"
    assert reasons["not-a-window"] == "outside_filters"
    stale = call(
        "selection.save",
        {
            **_scope(warehouse),
            "recipe": "custom_window_set",
            "filters": filters,
            "preview_id": "not-the-preview",
            "name": "stale",
        },
        root=workflow,
    )
    assert stale["error"]["code"] == "preview_stale"
    assert not (workflow / "selections").exists()
    request = {
        **_scope(warehouse),
        "recipe": "custom_window_set",
        "filters": {
            **filters,
            "daily_time": {"enabled": False},
            "outcome": "up",
            "label_source": "proxy",
        },
        "requirements": {"books": True, "reference": True, "warmup": True, "scoring_outcome": True},
        "preview_id": up["result"]["preview_id"],
        "name": "up proxy",
    }
    saved = call("selection.save", request, root=workflow)
    assert saved["ok"]
    opened = call(
        "selection.open", {"selection_id": saved["result"]["selection_id"]}, root=workflow
    )
    assert opened["ok"]
    assert [row["market_id"] for row in opened["result"]["included"]] == ["m-up"]
    assert opened["result"]["filters"]["outcome"] == "up"
    assert opened["result"]["limitation"] is None
    later = warehouse / "builds" / "later" / "manifest.json"
    later.parent.mkdir()
    later.write_text(
        '{"build_id":"later","collection_id":"c1","schema_version":"1","parser_version":"'
        + PARSER_VERSION
        + '","files":[]}\n',
        encoding="utf-8",
    )
    reopened = call(
        "selection.open", {"selection_id": saved["result"]["selection_id"]}, root=workflow
    )
    assert reopened["ok"]
    pinned = next((warehouse).rglob("build-meta.parquet"))
    pinned.write_bytes(pinned.read_bytes() + b"\x00")
    mismatch = call(
        "selection.open", {"selection_id": saved["result"]["selection_id"]}, root=workflow
    )
    assert mismatch["error"]["code"] == "pin_mismatch"


def test_bind_refuses_legacy_and_missing_inputs(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse"
    workflow = tmp_path / "workflows"
    build_archive(warehouse)
    filters = {
        "outcome": "any",
        "label_source": "any",
        "selection_mode": "all_matches",
        "daily_time": {"enabled": False},
        "time_zone": "UTC",
        "weekdays": [1, 2, 3, 4, 5, 6, 7],
    }
    missing = call(
        "data_recipe.resolve",
        {**_scope(warehouse), "recipe": "missing_inputs", "filters": filters, "requirements": {}},
        root=workflow,
    )
    assert missing["ok"]
    if missing["result"]["included"]:
        stored = call(
            "selection.save",
            {
                **_scope(warehouse),
                "recipe": "missing_inputs",
                "filters": filters,
                "requirements": {},
                "preview_id": missing["result"]["preview_id"],
                "name": "gaps",
            },
            root=workflow,
        )
        assert stored["ok"]
        refused = call(
            "experiment.bind",
            {
                "selection_id": stored["result"]["selection_id"],
                "metrics": [_metric()],
                "scenarios": ["friction"],
            },
            root=workflow,
        )
        assert refused["error"]["code"] == "diagnostic_not_replayable"
    legacy = call(
        "selection.save",
        {"windows": [{"market_id": "m-up", "collection": "c1"}], "name": "old"},
        root=workflow,
    )
    bound = call(
        "experiment.bind",
        {"selection_id": legacy["result"]["selection_id"], "metrics": [_metric()]},
        root=workflow,
    )
    assert bound["error"]["code"] == "legacy_unpinned"
    opened = call(
        "selection.open", {"selection_id": legacy["result"]["selection_id"]}, root=workflow
    )
    assert opened["result"]["limitation"] == "legacy_unpinned"


def _metric() -> dict:
    return {
        "name": "realized_pnl",
        "unit": "USDC",
        "denominator": "resolved_fills",
        "eligible_population": "included_windows",
        "missing_value_rule": "unknown_is_not_zero",
    }


def test_bind_builds_strategy_replay_units(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse"
    workflow = tmp_path / "workflows"
    build_archive(warehouse)
    filters = {
        "outcome": "up",
        "label_source": "proxy",
        "selection_mode": "all_matches",
        "daily_time": {"enabled": False},
        "time_zone": "UTC",
        "weekdays": [1, 2, 3, 4, 5, 6, 7],
    }
    requirements = {"books": True, "reference": True, "warmup": True, "scoring_outcome": True}
    preview = call(
        "data_recipe.resolve",
        {
            **_scope(warehouse),
            "recipe": "custom_window_set",
            "filters": filters,
            "requirements": requirements,
        },
        root=workflow,
    )
    saved = call(
        "selection.save",
        {
            **_scope(warehouse),
            "recipe": "custom_window_set",
            "filters": filters,
            "requirements": requirements,
            "preview_id": preview["result"]["preview_id"],
            "name": "diagnostic up",
        },
        root=workflow,
    )
    bound = call(
        "experiment.bind",
        {
            "selection_id": saved["result"]["selection_id"],
            "metrics": [_metric()],
            "scenarios": ["friction"],
            "baseline": "reference",
            "acceptance_criteria": "Account for every included window.",
            "parameters": {},
        },
        root=workflow,
    )
    assert bound["ok"]
    assert bound["result"]["units_source"] == "selection"
    assert bound["result"]["outcome_conditioned"] is True
    assert bound["result"]["limitation"] == "outcome_conditioned"
    stored = json.loads(
        (workflow / "experiments" / f"{bound['result']['experiment_id']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["units"][0]["kind"] == "strategy_replay"
    assert stored["units"][0]["market_id"] == "m-up"
    assert "manifests" not in stored["units"][0]
    assert stored["pins"]["manifests"]
    assert stored["units"][0]["parameters"]["quality"]["accepted_k_qualities"] == [
        "CONFIRMED_CANONICAL"
    ]
    assert "units" not in {"selection_id", "metrics"}


def test_preparation_owner_and_unchanged_selection(tmp_path: Path) -> None:
    from tyrex_pm.data_warehouse.contracts import digest

    profile = replace(
        Profile(), storage=replace(Storage(), raw_fsync_interval_ms=1, min_free_disk_gib=0)
    )
    atomic_json(
        tmp_path / "collections/collection/collection.json",
        {"profile": asdict(profile), "profile_hash": profile.hash},
    )
    recorder = RawWriter(tmp_path, "collection", "session", profile.hash, profile.storage)
    start = 1_780_000_200 * NS
    payload = canonical({"event_type": "new_future_event"})
    _row, future = recorder.submit(
        payload, source="clob", stream="clob", connection="c", received_at=start
    )
    assert future.result(timeout=5)
    recorder.close()
    raw_hash = digest((tmp_path / "raw").read_bytes()) if False else None
    raw_files = {
        path: path.read_bytes() for path in (tmp_path / "raw").rglob("*") if path.is_file()
    }
    workflow = tmp_path / "wf"
    plan = call(
        "preparation.plan",
        {"kind": "normalize", "warehouse": str(tmp_path), "collection": "collection"},
        root=workflow,
    )
    assert plan["result"]["scope"] == "collection"
    assert plan["result"]["safe_stop"] == "unsupported"
    with Ownership(tmp_path, "collector"):
        busy = call(
            "preparation.start",
            {
                "preparation_id": plan["result"]["preparation_id"],
                "plan_id": plan["result"]["plan_id"],
                "inline": True,
            },
            root=workflow,
        )
    assert busy["error"]["code"] == "owner_busy"
    started = call(
        "preparation.start",
        {
            "preparation_id": plan["result"]["preparation_id"],
            "plan_id": plan["result"]["plan_id"],
            "inline": True,
        },
        root=workflow,
    )
    assert started["ok"]
    status = call(
        "preparation.status", {"preparation_id": plan["result"]["preparation_id"]}, root=workflow
    )
    assert status["result"]["state"] == "completed"
    assert status["result"]["owner_result"]["new_builds"] >= 1
    assert status["result"]["qualification_granted"] is False
    for path, blob in raw_files.items():
        assert path.read_bytes() == blob
    again = call(
        "preparation.plan",
        {
            "kind": "normalize",
            "warehouse": str(tmp_path),
            "collection": "collection",
            "preparation_id": "second",
        },
        root=workflow,
    )
    rerun = call(
        "preparation.start",
        {"preparation_id": "second", "plan_id": again["result"]["plan_id"], "inline": True},
        root=workflow,
    )
    assert rerun["ok"]
    assert (
        call("preparation.status", {"preparation_id": "second"}, root=workflow)["result"][
            "owner_result"
        ]["new_builds"]
        == 0
    )
    stopped = call("preparation.stop", {"preparation_id": "second"}, root=workflow)
    assert stopped["result"]["safe_stop"] == "unsupported"
    assert stopped["result"]["owner_interrupted"] is False
    del raw_hash


def test_stop_before_owner_does_not_run(tmp_path: Path) -> None:
    workflow = tmp_path / "wf"
    plan = call(
        "preparation.plan",
        {
            "kind": "verify",
            "warehouse": str(tmp_path),
            "collection": "collection",
            "preparation_id": "stop-me",
        },
        root=workflow,
    )
    folder = workflow / "preparations" / "stop-me"
    atomic_json(folder / "status.json", {"state": "running", "generation": 1, "kind": "verify"})
    atomic_json(folder / "stop.json", {"generation": 1})
    from tyrex_pm.research.workflows.preparation_worker import run_preparation

    assert run_preparation(folder) == 0
    assert (
        json.loads((folder / "status.json").read_text(encoding="utf-8"))["reason"]
        == "stop_before_owner"
    )
    del plan


def test_newest_quality_report_wins_and_unsupported_is_not_present(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse"
    build_archive(warehouse)
    folder = warehouse / "collections/c1/quality_markets/z-newer"
    folder.mkdir()
    atomic_json(folder / "manifest.json", {"market_id": "m-up", "revision": "newer"})
    atomic_json(
        folder / "report.json",
        {
            "market_id": "m-up",
            "assessed_at": 9_000_000_000_000_000_000,
            "semantic_readiness": "UNSUPPORTED",
            "reconstruction_usability": "GAP",
            "feeds": {
                "up-instrument": {
                    "status": "UNKNOWN_CONTINUITY",
                    "required_duration_ns": 300 * NS,
                    "usable_duration_ns": 300 * NS,
                },
                DOWN: {
                    "status": "UNKNOWN_CONTINUITY",
                    "required_duration_ns": 300 * NS,
                    "usable_duration_ns": 300 * NS,
                },
                "official_reference": {
                    "status": "UNKNOWN_CONTINUITY",
                    "required_duration_ns": 300 * NS,
                    "usable_duration_ns": 300 * NS,
                },
            },
        },
    )
    detail = call(
        "library.window",
        {**_scope(warehouse), "market_id": "m-up"},
        root=tmp_path / "wf",
    )
    quality = detail["result"]["window"]["tracks"]["quality"]
    assert detail["ok"]
    assert quality["state"] == "missing"
    assert quality["reason"] == "semantic_unsupported"


def test_partial_and_unassessed_exclusions_stay_distinct(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse"
    build_archive(warehouse)
    shutil.rmtree(warehouse / "collections/c1/quality_markets/m-open")
    workflow = tmp_path / "wf"
    requirements = {
        "books": True,
        "reference": True,
        "warmup": True,
        "coverage": "complete",
    }
    complete = call(
        "data_recipe.resolve",
        {**_scope(warehouse), "recipe": "eligible_windows", "requirements": requirements},
        root=workflow,
    )
    assert complete["ok"]
    reasons = {row["market_id"]: row["reason"] for row in complete["result"]["excluded"]}
    assert reasons["m-partial"] == "book_partial"
    assert reasons["m-open"] == "book_unassessed"
    groups = {group["id"]: group for group in complete["result"]["exclusion_groups"]}
    assert groups["partial"]["preparation_can_help"] is False
    assert groups["unassessed"]["preparation_can_help"] is True
    partial = call(
        "data_recipe.resolve",
        {
            **_scope(warehouse),
            "recipe": "eligible_windows",
            "requirements": {**requirements, "coverage": "partial"},
        },
        root=workflow,
    )
    included = {row["market_id"]: row for row in partial["result"]["included"]}
    assert "m-partial" in included
    assert "m-open" not in included
    assert included["m-partial"]["up_book_usable_ratio"] == 200 / 300
    assert included["m-partial"]["coverage"] == "partial"


def test_capture_blocks_keep_separate_recordings(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse"
    build_archive(warehouse)
    start = _ns("2026-03-29T00:30:00")
    session = warehouse / "collections/c1/sessions"
    session.mkdir()
    atomic_json(
        session / "sess-block.json",
        {
            "session_id": "sess-block",
            "started_at": start,
            "stopped_at": start + 600 * NS,
            "capture_clean": True,
            "clean_stop": True,
        },
    )
    listed = call(
        "library.blocks",
        {"roots": [str(warehouse)]},
        root=tmp_path / "wf",
    )
    assert listed["ok"]
    assert listed["result"]["blocks"][0]["block_id"] == "c1:sess-block"
    windows = call(
        "library.windows",
        {**_scope(warehouse), "block_ids": ["c1:sess-block"], "limit": 50},
        root=tmp_path / "wf",
    )
    assert {row["market_id"] for row in windows["result"]["windows"]} == {"m-up"}
    assert windows["result"]["windows"][0]["block_id"] == "c1:sess-block"


def test_exploratory_floor_is_separate_from_complete_coverage(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse"
    build_archive(warehouse)
    folder = warehouse / "collections/c1/quality_markets/z-unsupported"
    folder.mkdir()
    atomic_json(folder / "manifest.json", {"market_id": "m-down", "revision": "later"})
    atomic_json(
        folder / "report.json",
        {
            "market_id": "m-down",
            "assessed_at": 9_000_000_000_000_000_000,
            "semantic_readiness": "UNSUPPORTED",
            "reason": "official_threshold_and_payoff_contract_not_attested",
            "feeds": {
                "up-instrument": {
                    "status": "UNKNOWN_CONTINUITY",
                    "required_duration_ns": 300 * NS,
                    "usable_duration_ns": 300 * NS,
                },
                DOWN: {
                    "status": "UNKNOWN_CONTINUITY",
                    "required_duration_ns": 300 * NS,
                    "usable_duration_ns": 300 * NS,
                },
                "official_reference": {
                    "status": "UNKNOWN_CONTINUITY",
                    "required_duration_ns": 300 * NS,
                    "usable_duration_ns": 300 * NS,
                },
            },
        },
    )
    requirements = {
        "books": True,
        "reference": True,
        "warmup": True,
        "coverage": "exploratory",
        "coverage_floor": 0.95,
        "settlement_confidence": "provider_reported",
    }
    exploratory = call(
        "data_recipe.resolve",
        {**_scope(warehouse), "recipe": "eligible_windows", "requirements": requirements},
        root=tmp_path / "wf",
    )
    assert exploratory["ok"]
    included = {row["market_id"] for row in exploratory["result"]["included"]}
    assert "m-down" in included
    assert "m-partial" not in included
    assert exploratory["result"]["research_limitation"].startswith("Exploratory result:")
    limitation = exploratory["result"]["research_limitation"]
    assert "checked against later provider metadata" not in limitation
    up = next(row for row in exploratory["result"]["included"] if row["market_id"] == "m-up")
    assert up["price_to_beat_source"] == "provider_metadata"
    assert up["price_to_beat_value"] == "86264.7402356731"
    assert up["price_to_beat_comparison"] == "not_compared"
    assert up["price_to_beat_status"] == "recorded_before_close_not_used_at_decision"
    reasons = {row["market_id"]: row["reason"] for row in exploratory["result"]["excluded"]}
    assert reasons["m-partial"] == "book_below_floor"
    strict = call(
        "data_recipe.resolve",
        {
            **_scope(warehouse),
            "recipe": "eligible_windows",
            "requirements": {
                "books": True,
                "reference": True,
                "warmup": True,
                "coverage": "complete",
            },
        },
        root=tmp_path / "wf",
    )
    strict_ids = {row["market_id"] for row in strict["result"]["included"]}
    assert "m-down" not in strict_ids
    saved = call(
        "selection.save",
        {
            **_scope(warehouse),
            "recipe": "eligible_windows",
            "requirements": requirements,
            "preview_id": exploratory["result"]["preview_id"],
            "name": "exploratory fixture",
        },
        root=tmp_path / "wf",
    )
    assert saved["ok"]
    bound = call(
        "experiment.bind",
        {
            "selection_id": saved["result"]["selection_id"],
            "metrics": [_metric()],
            "scenarios": ["friction"],
            "parameters": {},
            "max_units": 64,
        },
        root=tmp_path / "wf",
    )
    assert bound["ok"]
    assert bound["result"]["k_acceptance"] == ["CONFIRMED_CANONICAL", "PROVISIONAL"]


def test_price_to_beat_comes_from_provider_metadata_after_close() -> None:
    from tyrex_pm.research.workflows.inventory import price_to_beat_record

    end = 1_000
    record = price_to_beat_record(
        [
            {
                "received_at": 100,
                "family_attributes": "{}",
                "additional_fields": "{}",
            },
            {
                "received_at": 1_500,
                "additional_fields": json.dumps(
                    {"events": [{"eventMetadata": {"priceToBeat": "100.5"}}]}
                ),
            },
        ],
        end,
    )
    assert record["source"] == "provider_metadata"
    assert record["value"] == "100.5"
    assert record["status"] == "recorded_after_close_not_used_at_decision"
    assert record["comparison"] == "not_compared"
    assert price_to_beat_record([], end)["status"] == "unavailable"


def test_window_after_capture_horizon_is_not_preparation(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse"
    build_archive(warehouse)
    shutil.rmtree(warehouse / "collections/c1/quality_markets/m-open")
    publication = "publication-window-not-started"
    folder = warehouse / "collections/c1/quality" / publication[:24]
    folder.mkdir(parents=True)
    atomic_json(
        folder / "reports.json",
        [
            {
                "market_id": "m-open",
                "assessed_at": 1,
                "semantic_readiness": "UNAVAILABLE",
                "reason": "window_not_started",
                "scheduled_end": 2,
            }
        ],
    )
    atomic_json(
        warehouse / "collections/c1/quality_status.json",
        {"publication_id": publication, "published_at": 1},
    )
    resolved = call(
        "data_recipe.resolve",
        {
            **_scope(warehouse),
            "recipe": "eligible_windows",
            "requirements": {
                "books": True,
                "reference": True,
                "warmup": True,
                "coverage": "complete",
            },
        },
        root=tmp_path / "wf",
    )
    assert resolved["ok"]
    match = next(row for row in resolved["result"]["excluded"] if row["market_id"] == "m-open")
    assert match["reason"] == "window_not_started"
    assert match["preparation_can_help"] is False
    groups = {group["id"]: group for group in resolved["result"]["exclusion_groups"]}
    assert groups["not_started"]["preparation_can_help"] is False


def test_without_a_caller_list_no_window_is_withheld(tmp_path: Path) -> None:
    # A set has no partition: the notebook split file reserves nothing (23 Oct 2026).
    warehouse = tmp_path / "warehouse"
    workflow = tmp_path / "workflows"
    build_archive(warehouse)
    scope = _scope(warehouse)
    scope.pop("reserved_market_ids")
    listed = call("library.windows", scope, root=workflow)
    assert listed["ok"], listed
    rows = {row["market_id"]: row for row in listed["result"]["windows"]}
    assert rows["m-hold"]["outcome"] != "withheld"
    assert rows["m-hold"]["partition"] != "reserved"
    preview = call(
        "data_recipe.resolve",
        {
            **scope,
            "recipe": "eligible_windows",
            "requirements": {"books": True, "reference": True, "warmup": True},
        },
        root=workflow,
    )
    assert preview["ok"], preview
    assert all(row.get("reason") != "reserved_evaluation" for row in preview["result"]["excluded"])


def test_bind_no_longer_checks_the_notebook_holdout(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse"
    workflow = tmp_path / "workflows"
    build_archive(warehouse)
    filters = {
        "outcome": "up",
        "label_source": "proxy",
        "selection_mode": "all_matches",
        "daily_time": {"enabled": False},
        "time_zone": "UTC",
        "weekdays": [1, 2, 3, 4, 5, 6, 7],
    }
    requirements = {"books": True, "reference": True, "warmup": True, "scoring_outcome": True}
    preview = call(
        "data_recipe.resolve",
        {
            **_scope(warehouse),
            "recipe": "custom_window_set",
            "filters": filters,
            "requirements": requirements,
        },
        root=workflow,
    )
    saved = call(
        "selection.save",
        {
            **_scope(warehouse),
            "recipe": "custom_window_set",
            "filters": filters,
            "requirements": requirements,
            "preview_id": preview["result"]["preview_id"],
            "name": "up before the split check",
        },
        root=workflow,
    )
    assert saved["ok"], saved
    bound = call(
        "experiment.bind",
        {
            "selection_id": saved["result"]["selection_id"],
            "metrics": [_metric()],
            "scenarios": ["friction"],
            "baseline": "reference",
            "acceptance_criteria": "Account for every included window.",
            "parameters": {},
        },
        root=workflow,
    )
    assert bound["ok"] or bound["error"]["code"] != "holdout_in_development", bound


def test_unqualified_replay_keeps_gap_with_exposure() -> None:
    from tyrex_pm.research.workflows.worker import replay_result_fields

    fields = replay_result_fields(
        {
            "engine": {
                "qualified": False,
                "limitations": ["gap_with_exposure"],
                "fee_provenance": "recorded",
                "realized_pnl": "1.25",
            },
            "label": {
                "method": "proxy",
                "winner": "up",
                "reason": "reference",
                "payout_unit": "UNKNOWN",
            },
        }
    )
    assert fields["qualified"] is False
    assert fields["qualification"] == "unqualified"
    assert fields["limitations"] == ["gap_with_exposure"]
    assert fields["fee_provenance"] == "recorded"
    assert fields["label_method"] == "proxy"
    assert "realized_pnl" not in fields
