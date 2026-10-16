"""Token-level market changes reach exports; replay counts windows, not orders."""

from __future__ import annotations

from dataclasses import asdict, replace

import pyarrow.parquet as pq

from tyrex_pm.data_warehouse.capture.files import atomic_json, read_json
from tyrex_pm.data_warehouse.capture.writer import RawWriter
from tyrex_pm.data_warehouse.config import Profile, Storage
from tyrex_pm.data_warehouse.contracts import canonical, digest
from tyrex_pm.data_warehouse.normalize.worker import normalize_once
from tyrex_pm.data_warehouse.query.export import export_market
from tyrex_pm.data_warehouse.query.reader import Reader, exact_rows
from tyrex_pm.research.cli import run_replay

START = 1780000200 * 10**9
MARKET = "polymarket:condition:fictional-condition"
TOKEN = "polymarket:137:outcome:1"


def warehouse(root):
    profile = replace(
        Profile(), storage=replace(Storage(), min_free_disk_gib=0, raw_fsync_interval_ms=1)
    )
    atomic_json(
        root / "collections/c/collection.json",
        {"profile": asdict(profile), "profile_hash": profile.hash},
    )
    writer = RawWriter(root, "c", "s", profile.hash, profile.storage)
    documents = [
        (
            "gamma",
            START,
            {
                "id": "fictional",
                "conditionId": "fictional-condition",
                "slug": "btc-updown-5m-1780000200",
                "outcomes": ["Down", "Up"],
                "clobTokenIds": ["2", "1"],
                "description": "Fictional fixture.",
            },
        ),
        (
            "clob",
            START,
            {
                "event_type": "book",
                "asset_id": "1",
                "market": "fictional-condition",
                "bids": [{"price": "0.40", "size": "20"}],
                "asks": [{"price": "0.50", "size": "20"}],
            },
        ),
        (
            "clob",
            START + 10**8,
            {
                "event_type": "tick_size_change",
                "asset_id": "1",
                "market": "fictional-condition",
                "old_tick_size": "0.01",
                "new_tick_size": "0.001",
            },
        ),
    ]
    for source, at, body in documents:
        writer.submit(
            canonical(body), source=source, stream=source, connection="conn", received_at=at
        )[1].result(5)
    writer.close()
    normalize_once(root)


def test_token_tick_change_is_queried_and_exported(tmp_path):
    root = tmp_path / "warehouse"
    warehouse(root)
    reader = Reader(root, "c")
    assert [r["entity_id"] for r in reader.iter_rows("market_changes")] == [TOKEN]
    queried = [
        row
        for dataset, row in reader.read_observations(
            MARKET, START, START + 300 * 10**9, ("market_changes",)
        )
    ]
    assert [r["change_type"] for r in queried] == ["tick_size_change"]
    manifest = export_market(reader, MARKET, tmp_path / "export")
    files = [f for f in manifest["files"] if f["dataset"] == "market_changes"]
    assert files
    exported = exact_rows(pq.read_table(tmp_path / "export" / files[0]["path"]))
    assert [r["entity_id"] for r in exported] == [TOKEN]


def test_replay_counts_traded_windows_separately_from_orders(tmp_path):
    root = tmp_path / "warehouse"
    warehouse(root)
    export = tmp_path / "export"
    export_market(Reader(root, "c"), MARKET, export)
    atomic_json(
        tmp_path / "dataset.json",
        {
            "schema_version": 1,
            "exports": [
                {
                    "path": "export",
                    "manifest_sha256": digest((export / "manifest.json").read_bytes()),
                }
            ],
            "selection_policy": "fictional_fixture",
            "expected_windows": 1,
            "discovered_windows": 1,
        },
    )
    atomic_json(
        tmp_path / "intents.json",
        [
            {
                "order_id": order,
                "at_ns": START + offset,
                "instrument_id": TOKEN,
                "action": "BUY",
                "quantity": "1",
                "limit_price": "0.50",
            }
            for order, offset in (("one", 0), ("two", 5 * 10**7))
        ],
    )
    (tmp_path / "scenario.yaml").write_text(
        "intents_file: intents.json\nparticipation: '1'\norder_delay_ms: 0\n"
        "fee_policy:\n  rate: '0'\n  exponent: 1\n",
        encoding="utf-8",
    )
    run_replay(tmp_path / "dataset.json", tmp_path / "scenario.yaml", tmp_path / "experiment")
    counts = read_json(tmp_path / "experiment/result.json")["dataset_counts"]
    assert counts["traded"] == 1
    assert counts["filled_orders"] == 2
    assert counts["fills"] == 2
