"""Regression evidence for the Phase B completion review; all feeds are synthetic."""

from __future__ import annotations

import os
from dataclasses import asdict, replace
from decimal import Decimal
from unittest.mock import patch

import pyarrow.parquet as pq
import pytest

from tyrex_pm.data_warehouse.capture.files import atomic_json, read_json
from tyrex_pm.data_warehouse.capture.writer import RawWriter
from tyrex_pm.data_warehouse.config import Profile, Storage
from tyrex_pm.data_warehouse.contracts import canonical, digest
from tyrex_pm.data_warehouse.normalize.worker import compact, normalize_once
from tyrex_pm.data_warehouse.quality.coverage import coverage, transport_intervals
from tyrex_pm.data_warehouse.quality.publish import publish_quality
from tyrex_pm.data_warehouse.query.export import export_market, verify
from tyrex_pm.data_warehouse.query.reader import Reader, exact_rows, order
from tyrex_pm.data_warehouse.reconstruction.books import Books
from tyrex_pm.data_warehouse.supervisor import Collector
from tyrex_pm.research.cli import run_replay
from tyrex_pm.research.replay.config import Intent, Scenario
from tyrex_pm.research.replay.engine import Engine

T = 1780000200 * 10**9
MID = "polymarket:condition:fixture"
IID = "polymarket:137:outcome:1"
GAMMA = {
    "id": "fixture",
    "conditionId": "fixture",
    "slug": "btc-updown-5m-1780000200",
    "outcomes": ["Up", "Down"],
    "clobTokenIds": ["1", "2"],
    "description": "Synthetic rules",
}
BOOK = {
    "event_type": "book",
    "asset_id": "1",
    "market": "fixture",
    "bids": [{"price": "0.49", "size": "10"}],
    "asks": [{"price": "0.51", "size": "10"}],
}
RESOLUTION = {"data": [{"condition_id": "fixture", "status": "TEST_DONE", "payouts": ["1", "0"]}]}


def record(root, documents, session="s"):
    profile = replace(
        Profile(), storage=replace(Storage(), min_free_disk_gib=0, raw_fsync_interval_ms=1)
    )
    atomic_json(
        root / "collections/c/collection.json",
        {"profile": asdict(profile), "profile_hash": profile.hash},
    )
    writer = RawWriter(root, "c", session, profile.hash, profile.storage)
    for source, at, payload in documents:
        writer.submit(
            canonical(payload), source=source, stream=source, connection="conn", received_at=at
        )[1].result(5)
    writer.close()
    normalize_once(root)
    return profile


def attest(root):
    reader = Reader(root, "c")
    atomic_json(
        root / "collections/c/provider_policy.json",
        {
            "schema_version": 1,
            "policy_id": "test-only",
            "evidence": ["Synthetic test only"],
            "terminal_statuses": ["TEST_DONE"],
            "payout_unit": "USD_per_share",
            "payout_index_basis": "outcome_index",
            "rule_status": "SUPPORTED",
            "official_threshold": "ATTESTED",
            "supported_rule_fingerprints": [reader.get_market_history(MID)[-1]["rule_fingerprint"]],
        },
    )


def test_readiness_rejects_missing_feeds_in_verification_and_replay(tmp_path):
    root = tmp_path / "warehouse"
    record(root, [("gamma", T, GAMMA), ("clob", T, BOOK), ("resolution", T + 10**9, RESOLUTION)])
    attest(root)
    result = verify(root, "c", require_ready=True)
    report = result["market_coverage"][0]
    assert report["semantic_readiness"] == "READY"
    assert report["settlement_readiness"] == "PROVIDER_TERMINAL"
    assert not result["ok"]
    assert "feed:official_reference" in report["readiness"]["reasons"]
    export_market(Reader(root, "c"), MID, tmp_path / "export")
    atomic_json(
        tmp_path / "dataset.json",
        {
            "schema_version": 1,
            "exports": [
                {
                    "path": "export",
                    "manifest_sha256": digest((tmp_path / "export/manifest.json").read_bytes()),
                }
            ],
        },
    )
    (tmp_path / "scenario.yaml").write_text(
        "quality_predicate: strategy_ready\nintents_file: intents.json\n"
    )
    atomic_json(tmp_path / "intents.json", [])
    with pytest.raises(ValueError, match="strategy-ready"):
        run_replay(tmp_path / "dataset.json", tmp_path / "scenario.yaml", tmp_path / "experiment")
    assert not verify(tmp_path / "nonexistent")["ok"]


def test_repeated_metadata_retains_two_payout_labels(tmp_path):
    record(
        tmp_path,
        [
            ("gamma", T, GAMMA),
            ("gamma", T + 10**9, GAMMA),
            ("resolution", T + 2 * 10**9, RESOLUTION),
        ],
    )
    attest(tmp_path)
    report = coverage(Reader(tmp_path, "c"), MID)
    assert len(report["scoring_labels"]) == 2
    assert {r["instrument_id"]: r["payout"] for r in report["scoring_labels"]}[IID].startswith("1")


def test_export_carries_old_seed_and_does_not_extend_horizon_with_late_metadata(tmp_path):
    record(
        tmp_path,
        [
            ("gamma", T - 1000 * 10**9, GAMMA),
            ("clob", T - 901 * 10**9, BOOK),
            (
                "clob",
                T + 10**9,
                {
                    "event_type": "price_change",
                    "market": "fixture",
                    "price_changes": [
                        {"asset_id": "1", "side": "BUY", "price": "0.49", "size": "20"}
                    ],
                },
            ),
            ("gamma", T + 10000 * 10**9, GAMMA),
        ],
    )
    reader = Reader(tmp_path, "c")
    assert reader.book_as_observed(IID, T + 10**9)["valid"]
    manifest = export_market(reader, MID, tmp_path / "export")
    events = []
    for file in manifest["files"]:
        if file["path"].startswith("observations/"):
            events.extend(
                (file["dataset"], row)
                for row in exact_rows(pq.ParquetFile(tmp_path / "export" / file["path"]).read())
            )
    assert any(dataset == "book_snapshots" for dataset, _ in events)
    assert max(row["received_at"] for _, row in events) <= manifest["received_until"]
    result = Engine(replace(Scenario(), order_delay_ms=0, participation="1")).run(
        sorted(events, key=lambda event: order(event[1])),
        [Intent("buy", T + 10**9, IID, "BUY", "1", "0.51")],
    )
    assert any(row["event"] == "fill" for row in result["ledger"])
    context = read_json(tmp_path / "export/source_context.json")
    assert context["effective_profile"]["profile_id"] == "btc-5m"


def operation(at, session, **details):
    return {"received_at": at, "session_id": session, "details": canonical(details)}


def test_restart_bounds_reference_and_book_epochs_by_confirmed_evidence():
    rows = [
        operation(100, "old", type="connected", source="polybolt", connection_id="c"),
        operation(
            150,
            "old",
            type="heartbeat",
            status={
                "feeds": {"r": {"source": "polybolt", "connection_id": "c", "connected": True}}
            },
        ),
        operation(300, "new", type="session_interrupted", previous_session="old"),
        operation(400, "new", type="connected", source="polybolt", connection_id="d"),
        operation(500, "new", type="disconnected", source="polybolt", connection_id="d"),
    ]
    assert transport_intervals(rows, "polybolt", 600) == [(100, 150), (400, 500)]
    books = Books()
    books.apply(
        "book_snapshots",
        {
            "snapshot_origin": "websocket",
            "instrument_id": IID,
            "record_id": "seed",
            "received_at": 100,
            "session_id": "old",
            "connection_id": "c",
            "source_id": "clob",
            "bids": [{"price": Decimal(".4"), "quantity": Decimal(1)}],
            "asks": [{"price": Decimal(".6"), "quantity": Decimal(1)}],
        },
    )
    books.apply("operational_events", rows[1])
    books.apply("operational_events", rows[2])
    assert books.epochs[0]["invalidated_at"] == 150
    assert not books.books[IID].valid


@pytest.mark.asyncio
async def test_live_resolution_conflict_survives_later_matching_answer_and_restart(tmp_path):
    profile = replace(
        Profile(), storage=replace(Storage(), min_free_disk_gib=0, raw_fsync_interval_ms=1)
    )
    collector = Collector(tmp_path, profile, None, normalize_live=False)
    cid = collector.collection
    policy = {
        "schema_version": 1,
        "policy_id": "test",
        "evidence": ["synthetic"],
        "terminal_statuses": ["TEST_DONE"],
        "payout_unit": "USD_per_share",
    }
    atomic_json(collector.collection_dir / "provider_policy.json", policy)
    collector.provider_policy = policy
    collector.markets[MID] = {
        "market_id": MID,
        "condition_id": "fixture",
        "window_end": 0,
        "resolution_next_at": 0,
    }
    for n, (seconds, payouts) in enumerate(
        [(1, ["1", "0"]), (3601, ["0", "1"]), (3661, ["1", "0"])]
    ):
        at = seconds * 10**9

        async def read(*args, **kwargs):
            payload = {
                "data": [{"condition_id": "fixture", "status": "TEST_DONE", "payouts": payouts}]
            }
            raw = await collector.capture(
                canonical(payload), source="resolution", stream="resolution", received_at=at
            )
            return payload, raw

        with (
            patch.object(collector.http, "read", read),
            patch("tyrex_pm.data_warehouse.supervisor.time.time_ns", return_value=at),
        ):
            await collector.resolution_cycle()
        assert collector.markets[MID]["resolution_status"] == (
            "PROVIDER_TERMINAL" if n == 0 else "CONFLICTING"
        )
    collector.writer.close()
    collector.catalog.close()
    await collector.http.close()
    resumed = Collector(tmp_path, profile, None, collection=cid, normalize_live=False)
    assert resumed.markets[MID]["conflicting_terminal_observation"]
    assert resumed.markets[MID]["resolution_status"] == "CONFLICTING"
    resumed.writer.close()
    resumed.catalog.close()
    await resumed.http.close()


def test_compaction_streams_input_and_preserves_pinned_rows(tmp_path, monkeypatch):
    record(tmp_path, [("gamma", T, GAMMA), ("clob", T, BOOK)], "one")
    record(tmp_path, [("gamma", T + 10**9, GAMMA), ("clob", T + 10**9, BOOK)], "two")
    old = Reader(tmp_path, "c")
    expected = list(old.iter_rows("processing_results"))
    factory = pq.ParquetFile

    class StreamingOnly:
        def __init__(self, path):
            self.path, self.file = path, factory(path)

        def iter_batches(self, **kwargs):
            return self.file.iter_batches(**kwargs)

        def read(self):
            from pathlib import Path

            path = Path(getattr(self.path, "name", self.path))
            assert path.suffix == ".tmp", "compaction must not materialize input parts"
            return self.file.read()

    with monkeypatch.context() as scoped:
        scoped.setattr(pq, "ParquetFile", StreamingOnly)
        compact(tmp_path, "c", max_part_rows=2)
    new = Reader(tmp_path, "c")
    assert list(new.iter_rows("processing_results")) == expected
    assert list(old.iter_rows("processing_results")) == expected
    assert all(f["rows"] <= 2 for build in new.manifests for f in build["files"])
    assert all(build["logical_checksums"] for build in new.manifests)


def test_quality_publication_is_immutable_and_contains_coverage_lineage(tmp_path):
    record(tmp_path, [("gamma", T, GAMMA), ("clob", T, BOOK)])
    first = publish_quality(tmp_path, "c")
    second = publish_quality(tmp_path, "c")
    assert first == second
    datasets = {file["dataset"] for file in first["files"]}
    assert {
        "coverage_intervals",
        "market_coverage",
        "book_epochs",
        "boundary_observations",
    } <= datasets
    assert first["input_build_ids"]
    status = read_json(tmp_path / "collections/c/quality_status.json")
    assert status["publication_id"] == first["publication_id"]


def test_readiness_requires_all_dimensions_but_allows_declared_unknown_continuity():
    from tyrex_pm.data_warehouse.quality.readiness import readiness

    report = {
        "capture_integrity": "VERIFIED",
        "normalization": "ACCOUNTED",
        "normalization_complete": True,
        "semantic_readiness": "READY",
        "settlement_readiness": "PROVIDER_TERMINAL",
        "closure_status": "OBSERVED",
        "reconstruction_usability": "NO_DETECTED_GAP_WITH_STATED_ASSURANCE",
        "scoring_labels": [{"status": "PROVIDER_TERMINAL"}],
        "feeds": {
            "reference": {
                "required": True,
                "required_duration_ns": 100,
                "unusable_or_missing_duration_ns": 0,
                "status": "UNKNOWN_CONTINUITY",
            }
        },
    }
    assert readiness(report)["ready"]
    report["normalization_complete"] = False
    assert not readiness(report)["ready"]


def test_provider_fixture_manifest_checksums_and_decoder_contracts():
    from pathlib import Path

    from tyrex_pm.data_warehouse.contracts import RawEnvelope
    from tyrex_pm.data_warehouse.normalize.registry import normalize

    root = Path("tests/fixtures/data_warehouse")
    manifest = read_json(root / "manifest.json")
    assert not manifest["live_payloads"]
    for file in manifest["files"]:
        path = root / file["path"]
        assert digest(path.read_bytes()) == file["sha256"]
        for index, item in enumerate(read_json(path)):
            assert item["provenance"] == "synthetic"
            raw = RawEnvelope.create(
                body=canonical(item["payload"]),
                collection_id="fixture",
                session_id="fixture",
                sequence=index + 1,
                source=item["source"],
                stream="fixture",
                received_at=T,
                transport=item.get("transport", "websocket"),
                endpoint_id=item.get("endpoint"),
            )
            rows = list(normalize(raw))
            assert set(item["expected_datasets"]) <= {name for name, _ in rows}, item["name"]
            assert all(
                r["status"] != "REJECTED" for name, r in rows if name == "processing_results"
            ), item["name"]


@pytest.mark.asyncio
async def test_quality_worker_restarts_without_trading_or_network(tmp_path, monkeypatch):
    profile = replace(
        Profile(), storage=replace(Storage(), min_free_disk_gib=0, raw_fsync_interval_ms=1)
    )
    collector = Collector(tmp_path, profile, None)
    collector.session_path.parent.mkdir(parents=True, exist_ok=True)
    started = []

    class Process:
        returncode = None

        def poll(self):
            return self.returncode

    def spawn(args, **kwargs):
        assert "quality" in args and "--follow" in args
        process = Process()
        started.append(process)
        return process

    monkeypatch.setattr("tyrex_pm.data_warehouse.supervisor.subprocess.Popen", spawn)
    await collector.supervise_quality()
    await collector.supervise_quality()
    assert len(started) == 1
    started[0].returncode = 2
    collector.quality_retry_at = 0
    await collector.supervise_quality()
    assert len(started) == 2 and collector.quality_restarts == 1
    collector.quality_log.close()
    collector.writer.close()
    collector.catalog.close()
    await collector.http.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("drain_fails", [False, True])
async def test_supervised_workers_publish_final_sealed_data_without_network(
    tmp_path, monkeypatch, drain_fails
):
    profile = replace(
        Profile(), storage=replace(Storage(), min_free_disk_gib=0, raw_fsync_interval_ms=1)
    )
    collector = Collector(tmp_path, profile, None)
    await collector.capture(canonical(GAMMA), source="gamma", stream="fixture", received_at=T)

    async def idle(*args, **kwargs):
        await collector.stop.wait()

    async def no_op():
        return None

    monkeypatch.setattr("tyrex_pm.data_warehouse.supervisor.record_stream", idle)
    for owner, name in (
        (collector.discovery, "cycle"),
        (collector.discovery, "scan"),
        (collector, "metadata_cycle"),
        (collector, "book_audit"),
        (collector, "resolution_cycle"),
        (collector, "clocks"),
    ):
        monkeypatch.setattr(owner, name, no_op)
    if drain_fails:

        def fail():
            raise ValueError("synthetic drain failure")

        monkeypatch.setattr(collector, "drain_derived", fail)
    result = await collector.run(0.05)
    assert result["received"] == result["fsynced"]
    assert result["capture_clean"]
    assert result["derived_processing_complete"] is not drain_fails
    assert result["derived_processing_error"] == ("ValueError" if drain_fails else None)
    session = read_json(collector.session_path)
    assert session["capture_clean"]
    assert session["derived_processing_complete"] is not drain_fails
    report = read_json(collector.collection_dir / "quality_status.json")
    assert report["input_build_ids"] == sorted(
        b["build_id"] for b in Reader(tmp_path, collector.collection).manifests
    )
    assert collector.normalizer.poll() == 0
    assert collector.quality_worker.poll() == 0


def test_reader_opens_nonoverlapping_parts_only_when_needed(tmp_path, monkeypatch):
    record(tmp_path, [("gamma", T, GAMMA)], "one")
    record(tmp_path, [("gamma", T + 10**9, GAMMA)], "two")
    reader = Reader(tmp_path, "c")
    original = reader._part
    opened = []

    def tracked(file, start, until):
        opened.append(file["path"])
        yield from original(file, start, until)

    monkeypatch.setattr(reader, "_part", tracked)
    rows = reader.iter_rows("markets")
    assert next(rows)["received_at"] == T
    assert len(opened) == 1
    assert next(rows)["received_at"] == T + 10**9
    assert len(opened) == 2


def test_incremental_quality_index_keeps_old_seed_and_filters_other_books(tmp_path, monkeypatch):
    from tyrex_pm.data_warehouse.quality.index import CoverageIndex

    record(
        tmp_path,
        [("gamma", T, GAMMA), ("clob", T, BOOK), ("clob", T, {**BOOK, "asset_id": "unrelated"})],
    )
    index = CoverageIndex(Reader(tmp_path, "c"))
    index.update()
    initial = index.db.execute("SELECT sum(rows) FROM blocks").fetchone()[0]
    with patch("tyrex_pm.data_warehouse.quality.index.file_digest", side_effect=AssertionError):
        index.update()  # No immutable part is decoded or hashed twice.
    assert index.db.execute("SELECT sum(rows) FROM blocks").fetchone()[0] == initial
    rows = index.iter_rows("book_snapshots", entities={IID})
    assert {r["instrument_id"] for r in rows} == {IID}
    index.close()
    record(
        tmp_path, [("clob", T + 10**9, {**BOOK, "bids": [{"price": "0.48", "size": "9"}]})], "s2"
    )
    index = CoverageIndex(Reader(tmp_path, "c"))
    index.update()
    rows = list(index.iter_rows("book_snapshots", entities={IID}))
    assert len(rows) == 2
    assert rows[0]["received_at"] == T
    assert rows[1]["bids"][0]["price"] == Decimal("0.48")
    index.close()


def test_completed_window_coverage_does_not_extend_to_late_settlement(tmp_path):
    record(
        tmp_path,
        [("gamma", T, GAMMA), ("clob", T, BOOK), ("resolution", T + 900 * 10**9, RESOLUTION)],
    )
    report = coverage(Reader(tmp_path, "c"), MID, warmup_s=0)
    assert report["feeds"][IID]["required_until"] == T + 300 * 10**9
    assert report["book_epochs"][-1]["invalidated_at"] == T + 300 * 10**9


def test_index_rebuild_after_compaction_preserves_coverage(tmp_path):
    record(tmp_path, [("gamma", T, GAMMA), ("clob", T, BOOK)])
    record(tmp_path, [("clob", T + 10**9, BOOK)], "s2")
    before = coverage(Reader(tmp_path, "c"), MID)
    compact(tmp_path, "c")
    after = coverage(Reader(tmp_path, "c"), MID)
    assert before == after


def test_publication_prioritizes_completed_and_reuses_unchanged_window(tmp_path, monkeypatch):
    import tyrex_pm.data_warehouse.quality.publish as publisher

    active = {**GAMMA, "conditionId": "active", "slug": "btc-updown-5m-1780000500"}
    profile = record(
        tmp_path,
        [
            ("gamma", T, active),
            ("gamma", T, GAMMA),
            ("clob", T, BOOK),
            ("resolution", T + 301 * 10**9, RESOLUTION),
        ],
    )
    # CLOB constraint metadata follows Gamma but has no schedule. It must not
    # erase the known window for priority, boundary inputs, or cache identity.
    writer = RawWriter(tmp_path, "c", "constraints", profile.hash, profile.storage)
    writer.submit(
        canonical({"condition_id": "fixture", "closed": False}),
        source="clob",
        stream="metadata",
        connection="rest",
        endpoint_id="clob_market",
        received_at=T + 301 * 10**9 + 1,
    )[1].result(5)
    writer.close()
    normalize_once(tmp_path)
    calls = []
    original = publisher.coverage

    def tracked(reader, mid):
        calls.append(mid)
        return original(reader, mid)

    monkeypatch.setattr(publisher, "coverage", tracked)
    first = publish_quality(tmp_path, "c")
    assert calls[0] == MID
    calls.clear()
    record(tmp_path, [("gamma", T + 302 * 10**9, GAMMA)], "s2")
    second = publish_quality(tmp_path, "c")
    assert second["publication_id"] != first["publication_id"]
    assert MID not in calls


def test_publication_marks_new_unprocessed_sealed_input_pending(tmp_path):
    profile = record(tmp_path, [("gamma", T, GAMMA), ("clob", T, BOOK)])
    first = publish_quality(tmp_path, "c")
    assert first["normalization_complete"]
    writer = RawWriter(tmp_path, "c", "pending", profile.hash, profile.storage)
    writer.submit(
        canonical(BOOK), source="clob", stream="clob", connection="conn", received_at=T + 10**9
    )[1].result(5)
    writer.close()
    second = publish_quality(tmp_path, "c")
    assert second["publication_id"] != first["publication_id"]
    assert not second["normalization_complete"]


def test_live_command_returns_failure_for_incomplete_derived_drain(tmp_path, monkeypatch):
    from tyrex_pm.data_warehouse import cli

    class StoppedCollector:
        def __init__(self, *args, **kwargs):
            self.root, self.collection, self.session = tmp_path, "c", "s"

        async def run(self, duration, **_closing):
            return {
                "capture_clean": True,
                "derived_processing_complete": False,
                "derived_processing_error": "ValueError",
            }

    monkeypatch.setattr(cli, "load_credentials", lambda *a, **kw: None)
    monkeypatch.setattr("tyrex_pm.data_warehouse.supervisor.Collector", StoppedCollector)
    assert cli.live_main(["--root", str(tmp_path), "--duration", "1s"]) == 5


SECOND = {
    **GAMMA,
    "id": "second",
    "conditionId": "second",
    "slug": "btc-updown-5m-1780000500",
    "clobTokenIds": ["3", "4"],
}
SECOND_MID = "polymarket:condition:second"
S = 10**9


def depth(first, last, bid="60000", size="3"):
    return {"e": "depthUpdate", "U": first, "u": last, "b": [[bid, size]], "a": []}


def two_window_archive(root):
    """Two windows, one Binance connection per session, a restart, and a sequence gap."""
    early = [
        ("gamma", T - 1000 * S, GAMMA),
        ("gamma", T - 1000 * S, SECOND),
        ("binance", T - 950 * S, depth(101, 102)),
        ("binance", T - 949 * S, {"lastUpdateId": 100, "bids": [["59999", "2"]], "asks": []}),
        ("clob", T - 940 * S, BOOK),
        ("clob", T - 940 * S, {**BOOK, "asset_id": "3", "market": "second"}),
    ]
    early += [("binance", T - 900 * S + n * 10 * S, depth(102 + n, 102 + n)) for n in range(1, 100)]
    profile = record(root, early)
    writer = RawWriter(root, "c", "s2", profile.hash, profile.storage)
    writer.submit(
        canonical({"type": "session_interrupted", "previous_session": "s"}),
        source="collector",
        stream="collector",
        connection="collector",
        transport="collector_event",
        received_at=T + 120 * S,
    )[1].result(5)
    later = [
        ("binance", T + 121 * S, depth(301, 302)),
        ("binance", T + 122 * S, {"lastUpdateId": 300, "bids": [["59990", "1"]], "asks": []}),
        ("clob", T + 123 * S, BOOK),
        ("clob", T + 123 * S, {**BOOK, "asset_id": "3", "market": "second"}),
    ]
    later += [("binance", T + 125 * S + n * 10 * S, depth(302 + n, 302 + n)) for n in range(1, 30)]
    later.append(("binance", T + 420 * S, depth(900, 901)))  # sequence gap in window two
    later.append(("clob", T + 430 * S, BOOK))
    for source, at, payload in later:
        writer.submit(
            canonical(payload), source=source, stream=source, connection="conn", received_at=at
        )[1].result(5)
    writer.close()
    normalize_once(root)


def test_indexed_and_direct_coverage_agree_and_share_one_binance_replay(tmp_path):
    from tyrex_pm.data_warehouse.quality.coverage import coverage_source
    from tyrex_pm.data_warehouse.quality.index import FILENAME, CoverageIndex
    from tyrex_pm.data_warehouse.query.reader import BINANCE

    two_window_archive(tmp_path)
    index_path = tmp_path / "collections/c" / FILENAME
    direct = {m: coverage(Reader(tmp_path, "c"), m) for m in (MID, SECOND_MID)}
    assert not index_path.exists()  # Direct reads never create the index.
    publish_quality(tmp_path, "c")
    current = CoverageIndex.open_current(Reader(tmp_path, "c"))
    assert current is not None
    calls = []
    original = current.iter_rows

    def counting(dataset, *args, **kwargs):
        if kwargs.get("entities") == [BINANCE]:
            calls.append(dataset)
        return original(dataset, *args, **kwargs)

    current.iter_rows = counting
    with coverage_source(current) as source:
        indexed = {m: coverage(source, m) for m in (MID, SECOND_MID)}
    current.close()
    assert calls == ["book_snapshots", "book_updates"]  # One pass serves both windows.
    assert indexed == direct
    first, second = (direct[m]["feeds"]["binance_depth"] for m in (MID, SECOND_MID))
    assert 0 < first["usable_duration_ns"] < first["required_duration_ns"]  # restart gap
    assert second["status"] == "GAP"
    reasons = {e["reason"] for e in direct[SECOND_MID]["book_epochs"]}
    assert "sequence_gap" in reasons


def test_verify_and_export_do_not_write_or_lock_the_index(tmp_path):
    from tyrex_pm.data_warehouse.capture.files import Ownership
    from tyrex_pm.data_warehouse.quality.index import FILENAME

    record(tmp_path, [("gamma", T, GAMMA), ("clob", T, BOOK)])
    with Ownership(tmp_path, "quality-index"):  # A running quality worker owns it.
        result = verify(tmp_path, "c")
        export_market(Reader(tmp_path, "c"), MID, tmp_path / "export")
    assert result["market_coverage"]
    assert not (tmp_path / "collections/c" / FILENAME).exists()


def test_index_is_used_only_when_current_and_rebuilds_on_projection_change(tmp_path, monkeypatch):
    import tyrex_pm.data_warehouse.quality.index as module

    record(tmp_path, [("gamma", T, GAMMA), ("clob", T, BOOK)])
    publish_quality(tmp_path, "c")
    current = module.CoverageIndex.open_current(Reader(tmp_path, "c"))
    assert current is not None
    current.close()
    record(tmp_path, [("clob", T + S, BOOK)], "s2")
    assert module.CoverageIndex.open_current(Reader(tmp_path, "c")) is None  # stale
    index = module.CoverageIndex(Reader(tmp_path, "c"))
    index.update()
    parts = index.db.execute("SELECT count(*) FROM parts").fetchone()[0]
    index.close()
    monkeypatch.setattr(module, "PROJECTION_ID", "changed-projection")
    assert module.CoverageIndex.open_current(Reader(tmp_path, "c")) is None
    index = module.CoverageIndex(Reader(tmp_path, "c"))
    index.update()
    assert dict(index.db.execute("SELECT key, value FROM meta"))["projection"] == (
        "changed-projection"
    )
    assert index.db.execute("SELECT count(*) FROM parts").fetchone()[0] == parts
    with pytest.raises(ValueError):
        list(index.iter_rows("book_updates"))  # split rows require an entity filter
    index.close()


def price_change(token="1", price="0.48", size="5"):
    return {
        "event_type": "price_change",
        "market": "fixture",
        "price_changes": [{"asset_id": token, "side": "BUY", "price": price, "size": size}],
    }


def test_overlapping_clob_sockets_keep_independent_books(tmp_path):
    """A token briefly on two sockets: neither stream's updates erase the other's book."""
    profile = record(tmp_path, [("gamma", T - 600 * S, GAMMA)])
    writer = RawWriter(tmp_path, "c", "s2", profile.hash, profile.storage)

    def send(at, payload, connection, source="clob", **kwargs):
        writer.submit(
            canonical(payload),
            source=source,
            stream=source,
            connection=connection,
            received_at=at,
            **kwargs,
        )[1].result(5)

    send(T - 20 * S, BOOK, "a")
    send(T - 10 * S, BOOK, "b")
    for n in range(30):  # both sockets keep streaming the same token
        send(T + n * 10 * S, price_change(size=str(5 + n)), "a" if n % 2 else "b")
    send(
        T + 150 * S,
        {"type": "subscription", "connection_id": "a", "tokens": ["1"], "operation": "unsubscribe"},
        "collector",
        source="collector",
        transport="collector_event",
    )
    writer.close()
    normalize_once(tmp_path)
    report = coverage(Reader(tmp_path, "c"), MID, warmup_s=0)
    feed = report["feeds"][IID]
    assert feed["usable_duration_ns"] == feed["required_duration_ns"]
    reasons = [e["reason"] for e in report["book_epochs"] if e["instrument_id"] == IID]
    assert "connection_without_seed" not in reasons
    assert "unsubscribed" in reasons  # socket a's copy stops at its unsubscribe


def test_unsubscribed_stream_hands_over_and_does_not_stay_valid():
    books = Books()

    def snapshot(connection, at):
        return {
            "snapshot_origin": "websocket",
            "instrument_id": IID,
            "record_id": f"seed-{connection}",
            "received_at": at,
            "session_id": "s",
            "connection_id": connection,
            "source_id": "clob",
            "bids": [{"price": Decimal(".4"), "quantity": Decimal(1)}],
            "asks": [{"price": Decimal(".6"), "quantity": Decimal(1)}],
        }

    books.apply("book_snapshots", snapshot("a", 1))
    books.apply("book_snapshots", snapshot("b", 2))
    assert books.books[IID].connection_id == "a"  # a valid selection stays stable
    books.apply(
        "operational_events",
        {
            "received_at": 3,
            "session_id": "s",
            "details": canonical(
                {
                    "type": "subscription",
                    "connection_id": "a",
                    "tokens": ["1"],
                    "operation": "unsubscribe",
                }
            ),
        },
    )
    assert books.books[IID].valid and books.books[IID].connection_id == "b"
    assert books.epochs[-1]["reason"] == "unsubscribed"
    books.apply(
        "book_updates",
        {
            "received_at": 4,
            "session_id": "s",
            "connection_id": "a",
            "source_id": "clob",
            "changes": [
                {"instrument_id": IID, "side": "BUY", "price": Decimal(".5"), "new_quantity": 1}
            ],
        },
    )
    assert Decimal(".5") not in books.books[IID].bids  # late update on a is not applied to b


def test_shard_assignment_is_sticky_across_market_rolls():
    from tyrex_pm.data_warehouse.supervisor import assign_shards

    tokens = [f"{n:03d}" for n in range(300)]
    first = assign_shards({}, tokens, 200)
    rolled = [t for t in tokens if t != "000"] + ["299a"]  # one market leaves, one arrives
    second = assign_shards(first, sorted(rolled), 200)
    assert all(second[t] == first[t] for t in rolled if t in first)
    assert second["299a"] == 0  # fills the freed slot
    assert max(list(second.values()).count(s) for s in (0, 1)) <= 200
    shrunk = assign_shards(second, sorted(rolled)[:150], 200)
    assert set(shrunk.values()) == {0}


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing-violation retry")
def test_atomic_publication_retries_windows_sharing_violation(tmp_path, monkeypatch):
    import tyrex_pm.data_warehouse.capture.files as files

    real, failures = os.replace, []

    def flaky(source, target):
        if len(failures) < 2:
            failures.append(target)
            raise PermissionError(5, "Access is denied")
        real(source, target)

    monkeypatch.setattr(files.os, "replace", flaky)
    atomic_json(tmp_path / "status.json", {"ok": True})
    assert read_json(tmp_path / "status.json") == {"ok": True}
    assert len(failures) == 2


@pytest.mark.asyncio
async def test_resumed_session_runway_uses_only_its_own_growth(tmp_path, monkeypatch):
    import time

    import tyrex_pm.data_warehouse.supervisor as supervisor

    profile = replace(
        Profile(), storage=replace(Storage(), min_free_disk_gib=0, raw_fsync_interval_ms=1)
    )
    existing = 4 * 1024**3  # bytes from earlier sessions of the same collection
    monkeypatch.setattr(supervisor, "archive_bytes", lambda root: existing)
    collector = Collector(tmp_path, profile, None, normalize_live=False)
    collector.started_at = time.time_ns() - 100 * 10**9
    monkeypatch.setattr(supervisor, "archive_bytes", lambda root: existing + 10**6)
    await collector.heartbeat()
    per_day = read_json(collector.status_path)["raw_bytes_per_day"]
    assert 8e8 < per_day <= 10**6 * 864  # 1 MB in ~100 s, not 4 GiB in 100 s
    collector.writer.close()
    collector.catalog.close()
    await collector.http.close()
