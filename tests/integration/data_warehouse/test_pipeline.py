"""An offline recording goes through the actual CLI export/replay boundaries."""
from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path

import pytest

from tyrex_pm.data_warehouse.capture.files import atomic_json, read_json
from tyrex_pm.data_warehouse.capture.writer import RawWriter
from tyrex_pm.data_warehouse.cli import main
from tyrex_pm.data_warehouse.config import Profile, Storage
from tyrex_pm.data_warehouse.contracts import canonical, digest
from tyrex_pm.data_warehouse.normalize.worker import normalize_once
from tyrex_pm.data_warehouse.query.reader import Reader
from tyrex_pm.research.cli import run_replay


def test_record_export_replay_and_manifest_tamper(tmp_path):
    root = tmp_path / "warehouse"
    start = 1780000200 * 10**9
    profile = replace(Profile(), storage=replace(Storage(), min_free_disk_gib=0,
                                               raw_fsync_interval_ms=1))
    atomic_json(root / "collections/c/collection.json", {"profile": asdict(profile),
                                                        "profile_hash": profile.hash})
    writer = RawWriter(root, "c", "s", profile.hash, profile.storage)
    documents = [
        ("gamma", start - 10**9, {"id": "fictional", "conditionId": "fictional-condition",
            "slug": "btc-updown-5m-1780000200", "outcomes": ["Down", "Up"],
            "clobTokenIds": ["2", "1"],
            "description": "Fictional fixture; no live settlement claim."}),
        ("clob", start, {"event_type": "book", "asset_id": "1", "market": "fictional-condition",
            "bids": [{"price": "0.49", "size": "50"}], "asks": [{"price": "0.51", "size": "10"}]}),
        ("clob", start + 10**9, {
            "event_type": "book", "asset_id": "2", "market": "fictional-condition",
            "bids": [{"price": "0.48", "size": "50"}], "asks": [{"price": "0.52", "size": "10"}]}),
        ("clob", start + 5 * 10**9, {"event_type": "last_trade_price", "asset_id": "1",
            "price": "0.51", "size": "1", "side": "BUY"}),
    ]
    for source, at, body in documents:
        _, future = writer.submit(canonical(body), source=source, stream=source,
                                   connection="conn", received_at=at)
        future.result(5)
    writer.close()
    normalize_once(root)
    market = "polymarket:condition:fictional-condition"
    export = tmp_path / "export"
    assert main(["export-market", "--root", str(root), "--market", market,
                 "--output", str(export)]) == 0
    assert main(["verify", "--root", str(root), "--require-ready"]) == 1
    atomic_json(tmp_path / "dataset.json", {"schema_version": 1,
        "exports": [{
            "path": "export",
            "manifest_sha256": digest((export / "manifest.json").read_bytes()),
        }],
        "selection_policy": "fictional_fixture", "expected_windows": 1})
    atomic_json(tmp_path / "intents.json", [{"order_id": "one", "at_ns": start + 2 * 10**9,
        "instrument_id": "polymarket:137:outcome:1", "action": "BUY",
        "quantity": "5", "limit_price": "0.52"}])
    (tmp_path / "scenario.yaml").write_text("intents_file: intents.json\nparticipation: '1'\n")
    run_replay(tmp_path / "dataset.json", tmp_path / "scenario.yaml", tmp_path / "experiment-1")
    run_replay(tmp_path / "dataset.json", tmp_path / "scenario.yaml", tmp_path / "experiment-2")
    first = read_json(tmp_path / "experiment-1/result.json")
    second = read_json(tmp_path / "experiment-2/result.json")
    assert first == second
    assert len([r for r in first["ledger"] if r["event"] == "fill"]) == 1
    assert "order_constraints_unqualified" in first["limitations"]
    assert first["dataset_counts"]["exported"] == 1
    assert Reader(root).list_markets()[0]["market_id"] == market
    (export / "quality.json").write_text("{}")
    with pytest.raises(ValueError, match="sidecar checksum"):
        run_replay(tmp_path / "dataset.json", tmp_path / "scenario.yaml", tmp_path / "tampered")


def test_trading_import_does_not_load_warehouse_extras():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-c", (
        "import sys; import tyrex_pm.application.cli; "
        "assert not any(n.split('.')[0] in {'pyarrow','duckdb','zstandard'} for n in sys.modules); "
        "assert not any(n.startswith('tyrex_pm.data_warehouse') for n in sys.modules)"
    )], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_warehouse_has_no_execution_or_strategy_imports():
    import ast
    package = Path("src/tyrex_pm/data_warehouse")
    forbidden = ("tyrex_pm.execution", "tyrex_pm.runtime", "tyrex_pm.strategies")
    for file in package.rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(forbidden), file
            if isinstance(node, ast.Import):
                assert not any(alias.name.startswith(forbidden) for alias in node.names), file
