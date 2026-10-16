"""Replay scores qualified resolution rows after the decision loop."""

from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal

from tyrex_pm.data_warehouse.capture.files import atomic_json, read_json
from tyrex_pm.data_warehouse.capture.writer import RawWriter
from tyrex_pm.data_warehouse.config import Profile, Storage
from tyrex_pm.data_warehouse.contracts import canonical, digest
from tyrex_pm.data_warehouse.normalize.worker import normalize_once
from tyrex_pm.research.cli import run_replay


def test_qualified_payout_is_scored_once_and_does_not_fund_the_trade(tmp_path):
    root = tmp_path / "warehouse"
    start = 1780000200 * 10**9
    profile = replace(
        Profile(), storage=replace(Storage(), min_free_disk_gib=0, raw_fsync_interval_ms=1)
    )
    atomic_json(
        root / "collections/c/collection.json",
        {"profile": asdict(profile), "profile_hash": profile.hash},
    )
    atomic_json(
        root / "collections/c/provider_policy.json",
        {
            "schema_version": 1,
            "policy_id": "fixture-terminal",
            "evidence": ["synthetic fixture; not a live provider attestation"],
            "terminal_statuses": ["TEST_TERMINAL"],
            "payout_unit": "USD_per_share",
            "payout_index_basis": "outcome_index",
            "rule_status": "UNSUPPORTED",
            "official_threshold": "UNAVAILABLE",
        },
    )
    writer = RawWriter(root, "c", "s", profile.hash, profile.storage)
    documents = [
        (
            "gamma",
            start,
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
            start,
            {
                "event_type": "book",
                "asset_id": "1",
                "market": "fictional-condition",
                "bids": [{"price": "0.40", "size": "20"}],
                "asks": [{"price": "0.50", "size": "20"}],
            },
        ),
        (
            "resolution",
            start + 10**9,
            {
                "data": [{
                    "condition_id": "fictional-condition",
                    "status": "TEST_TERMINAL",
                    "payouts": ["0", "1"],
                }],
            },
        ),
    ]
    for source, at, body in documents:
        _row, future = writer.submit(
            canonical(body), source=source, stream=source, connection="conn", received_at=at
        )
        future.result(5)
    writer.close()
    normalize_once(root)
    export = tmp_path / "export"
    from tyrex_pm.data_warehouse.cli import main

    market = "polymarket:condition:fictional-condition"
    assert main([
        "export-market", "--root", str(root), "--market", market, "--output", str(export),
    ]) == 0
    quality = read_json(export / "quality.json")
    assert quality["settlement_readiness"] == "PROVIDER_TERMINAL"
    assert quality["scoring_labels"]
    atomic_json(
        tmp_path / "dataset.json",
        {
            "schema_version": 1,
            "exports": [{
                "path": "export",
                "manifest_sha256": digest((export / "manifest.json").read_bytes()),
            }],
            "selection_policy": "fictional_fixture",
            "expected_windows": 1,
            "discovered_windows": 1,
        },
    )
    atomic_json(
        tmp_path / "intents.json",
        [
            {
                "order_id": "one",
                "at_ns": start,
                "instrument_id": "polymarket:137:outcome:1",
                "action": "BUY",
                "quantity": "2",
                "limit_price": "0.50",
            }
        ],
    )
    (tmp_path / "scenario.yaml").write_text(
        "intents_file: intents.json\nparticipation: '1'\norder_delay_ms: 0\n"
        "fee_policy:\n  rate: '0'\n  exponent: 1\n",
        encoding="utf-8",
    )
    run_replay(tmp_path / "dataset.json", tmp_path / "scenario.yaml", tmp_path / "experiment")
    result = read_json(tmp_path / "experiment/result.json")
    receivables = [row for row in result["ledger"] if row["event"] == "payout_receivable"]
    paid = [row for row in receivables if Decimal(row["amount"]) > 0]
    assert len(paid) == 1
    assert paid[0]["instrument_id"] == "polymarket:137:outcome:1"
    assert Decimal(result["payout_receivables"]["polymarket:137:outcome:1"]) == Decimal("2")
    assert Decimal(result["cash"]) == Decimal("99")
    assert result["dataset_counts"]["traded"] == 1
    assert result["dataset_counts"]["unresolved"] == 0
    assert result["dataset_counts"]["discovered"] == 1
