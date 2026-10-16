"""Offline gates for discovery, coverage, publication stability, and extension."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from decimal import Decimal

import pyarrow.parquet as pq
import pytest

from tyrex_pm.data_warehouse.capture.files import atomic_json
from tyrex_pm.data_warehouse.capture.writer import RawWriter
from tyrex_pm.data_warehouse.catalog.sqlite import Catalog
from tyrex_pm.data_warehouse.config import Discovery, Profile, Storage
from tyrex_pm.data_warehouse.contracts import canonical
from tyrex_pm.data_warehouse.discovery import DiscoveryService
from tyrex_pm.data_warehouse.normalize.registry import register_external
from tyrex_pm.data_warehouse.normalize.worker import normalize_once
from tyrex_pm.data_warehouse.quality.coverage import interval_report, reference_intervals
from tyrex_pm.data_warehouse.query.export import export_market
from tyrex_pm.data_warehouse.query.reader import Reader, exact_rows
from tyrex_pm.data_warehouse.reconstruction.boundaries import resolution_assessment
from tyrex_pm.data_warehouse.reconstruction.policy import semantic_status
from tyrex_pm.data_warehouse.supervisor import Collector

START = 1780000200 * 10**9
MID = "polymarket:condition:fictional-condition"


class CatalogMemory:
    def __init__(self):
        self.rows = {}

    def values(self, _collection, namespace):
        return {key: value for (ns, key), value in self.rows.items() if ns == namespace}


class Host:
    def __init__(self, cap: int):
        self.collection = "c"
        self.profile = replace(
            Profile(),
            discovery=replace(Discovery(), max_selected_markets=cap, lookahead_s=300),
        )
        self.markets = {}
        self.catalog = CatalogMemory()
        self.events = []
        self.http = Http()

    def active_markets(self):
        return list(self.markets.values())

    async def persist(self, namespace, key, value):
        self.catalog.rows[(namespace, key)] = value

    async def persist_batch(self, updates):
        for update in updates:
            await self.persist(update["namespace"], update["key"], update["value"])

    async def event(self, event_type, **details):
        self.events.append((event_type, details))

    async def bind_feeds(self, _entity):
        return None


class Http:
    def __init__(self):
        self.mode = "found"

    async def read(self, endpoint, params=None, **_kwargs):
        if self.mode == "fail":
            raise ValueError("provider down")
        if endpoint == "gamma_keyset":
            return {"markets": [], "next_cursor": None}, Raw(1)
        slug = (params or {})["slug"]
        if self.mode == "empty":
            return [], Raw(1)
        return [
            {
                "id": slug,
                "slug": slug,
                "conditionId": slug,
                "outcomes": ["Down", "Up"],
                "clobTokenIds": ["101", "202"],
                "description": "synthetic",
            }
        ], Raw(1)


class Raw:
    def __init__(self, received_at):
        self.received_at = received_at
        self.raw_record_id = "raw"


def statuses(host: Host) -> dict[str, str]:
    return {key: value["status"] for key, value in host.catalog.values("c", "expected").items()}


def test_reference_coverage_uses_the_connection_span():
    span = reference_intervals([(0, 60_000_000_000)], [{"received_at": 1, "delivery_kind": "live"}])
    report = interval_report(span, 0, 60_000_000_000, assurance="UNKNOWN_CONTINUITY")
    assert report["usable_duration_ns"] == 60_000_000_000
    assert report["status"] == "UNKNOWN_CONTINUITY"
    assert report["staleness_is_coverage_gap"] is False


def test_semantic_readiness_follows_the_policy_file_only():
    assert semantic_status({}, "abc") == (
        "UNSUPPORTED",
        "official_threshold_and_payoff_contract_not_attested",
    )
    ready = semantic_status(
        {
            "rule_status": "SUPPORTED",
            "official_threshold": "ATTESTED",
            "supported_rule_fingerprints": ["abc"],
        },
        "abc",
    )
    assert ready == ("READY", None)
    assert semantic_status(
        {
            "rule_status": "SUPPORTED",
            "official_threshold": "ATTESTED",
            "supported_rule_fingerprints": ["abc"],
        },
        "other",
    )[0] == "UNSUPPORTED"


def test_resolution_empty_and_contradictory_states():
    assert resolution_assessment([])["status"] == "PENDING"
    rows = [
        {
            "record_id": "a",
            "received_at": 1,
            "provider_status": "DONE",
            "payouts": [Decimal(1), Decimal(0)],
        },
        {
            "record_id": "b",
            "received_at": 2,
            "provider_status": "DONE",
            "payouts": [Decimal(0), Decimal(1)],
        },
    ]
    assessed = resolution_assessment(
        rows, supported_statuses=("DONE",), payout_unit="USD_per_share"
    )
    assert assessed["status"] == "CONFLICTING"


def test_failed_probe_keeps_a_found_window_and_scan_failure_is_not_absence():
    host = Host(cap=8)
    service = DiscoveryService(host)
    asyncio.run(service.cycle())
    found = {slug for slug, status in statuses(host).items() if status == "FOUND"}
    assert found
    host.http.mode = "fail"
    asyncio.run(service.cycle())
    assert found <= {slug for slug, status in statuses(host).items() if status == "FOUND"}
    assert any(name == "discovery_probe_failed" for name, _details in host.events)
    host.http.mode = "empty"
    asyncio.run(service.cycle())
    assert found <= {slug for slug, status in statuses(host).items() if status == "FOUND"}
    host.http.mode = "fail"
    asyncio.run(service.scan())
    assert host.catalog.values("c", "tasks")["discovery_scan"]["absence_assurance"] == (
        "REQUEST_FAILED_NOT_PROOF_OF_ABSENCE"
    )


def test_capacity_exclusion_and_empty_listing_are_distinct():
    host = Host(cap=1)
    service = DiscoveryService(host)
    asyncio.run(service.cycle())
    values = list(statuses(host).values())
    assert values.count("FOUND") == 1
    assert "CAPACITY_EXCLUDED" in values
    host = Host(cap=8)
    host.http.mode = "empty"
    service = DiscoveryService(host)
    asyncio.run(service.cycle())
    assert set(statuses(host).values()) <= {"DISCOVERY_UNCERTAIN", "NOT_YET_LISTED"}
    host.http.mode = "found"
    asyncio.run(service.scan())
    assurance = host.catalog.values("c", "tasks")["discovery_scan"]["absence_assurance"]
    assert assurance.startswith("COMPLETE")


def test_open_journal_rebuilds_without_waiting_for_seal(tmp_path):
    profile = replace(
        Profile(), storage=replace(Storage(), raw_fsync_interval_ms=1, min_free_disk_gib=0)
    )
    atomic_json(
        tmp_path / "collections/c/collection.json",
        {"profile_hash": profile.hash, "profile": asdict(profile)},
    )
    writer = RawWriter(tmp_path, "c", "s", profile.hash, profile.storage)
    _row, future = writer.submit(
        canonical({
            "type": "projection",
            "namespace": "tasks",
            "key": "pending",
            "value": {"status": "PENDING"},
        }),
        source="collector",
        stream="operations",
        transport="collector_event",
    )
    future.result(5)
    catalog = Catalog(tmp_path)
    catalog.rebuild()
    assert catalog.values("c", "tasks")["pending"]["status"] == "PENDING"
    catalog.close()
    writer.close()


def test_late_reference_stays_invisible_and_exports_match(tmp_path):
    profile = replace(
        Profile(), storage=replace(Storage(), raw_fsync_interval_ms=1, min_free_disk_gib=0)
    )
    atomic_json(
        tmp_path / "collections/collection/collection.json",
        {"profile_hash": profile.hash, "profile": asdict(profile)},
    )
    writer = RawWriter(tmp_path, "collection", "session", profile.hash, profile.storage)

    def submit(payload, source, at, **kwargs):
        _row, future = writer.submit(
            canonical(payload),
            source=source,
            stream=source,
            connection="c",
            received_at=at,
            **kwargs,
        )
        future.result(5)

    submit(
        {
            "id": "fixture",
            "slug": "btc-updown-5m-1780000200",
            "conditionId": "fictional-condition",
            "outcomes": ["Down", "Up"],
            "clobTokenIds": ["2", "1"],
            "description": "Synthetic rules, not official.",
        },
        "gamma",
        START,
    )
    submit(
        {
            "type": "feed_binding",
            "binding": {
                "binding_id": "binding",
                "market_id": MID,
                "feed_id": "chainlink:btcusd:twap:60",
                "feed_version": "1",
                "role": "settlement_reference",
                "required": True,
                "applicable_from": START,
                "rule_version_id": "rule",
                "binding_evidence_ids": ["evidence"],
            },
        },
        "collector",
        START,
        transport="collector_event",
    )
    for offset, value in ((0, "100"), (10**9, "200")):
        submit(
            {
                "channel": "price.crypto.twap",
                "seq": 1,
                "payload": {
                    "symbol": "btcusd",
                    "source": "chainlink",
                    "timestamp": (START + offset) // 10**6,
                    "full_accuracy_value": value,
                },
            },
            "polybolt",
            START + offset,
        )
    writer.close()
    normalize_once(tmp_path)
    reader = Reader(tmp_path, "collection")
    early = reader.references_as_observed(MID, START)
    assert [row["value"] for row in early] == [Decimal("100")]
    first = tmp_path / "export-a"
    second = tmp_path / "export-b"
    export_market(reader, MID, first)
    export_market(reader, MID, second)
    assert (first / "quality.json").read_bytes() == (second / "quality.json").read_bytes()
    assert (first / "boundaries.json").read_bytes() == (second / "boundaries.json").read_bytes()
    for name in ("boundary_observations", "book_epochs", "resolution_assessments"):
        left = exact_rows(pq.ParquetFile(first / "derived" / f"{name}.parquet").read())
        right = exact_rows(pq.ParquetFile(second / "derived" / f"{name}.parquet").read())
        assert left == right


def test_windowless_family_and_non_price_payload_round_trip(tmp_path):
    register_external("fixture_text_note", "1", lambda payload: None)
    profile = replace(
        Profile(), storage=replace(Storage(), raw_fsync_interval_ms=1, min_free_disk_gib=0)
    )
    atomic_json(
        tmp_path / "collections/collection/collection.json",
        {"profile_hash": profile.hash, "profile": asdict(profile)},
    )
    writer = RawWriter(tmp_path, "collection", "session", profile.hash, profile.storage)
    documents = [
        (
            "gamma",
            {
                "market_family": "event_driven_question",
                "id": "question",
                "conditionId": "question-1",
                "slug": "will-the-fixture-resolve",
                "outcomes": ["Yes", "No"],
                "clobTokenIds": ["11", "22"],
                "description": "No scheduled window.",
            },
        ),
        (
            "external",
            {
                "feed_id": "fixture-note",
                "subject_id": "fixture",
                "observation_type": "note",
                "payload_schema_id": "fixture_text_note",
                "payload_schema_version": "1",
                "payload": {"text": "rain"},
            },
        ),
    ]
    for source, body in documents:
        _row, future = writer.submit(
            canonical(body), source=source, stream=source, received_at=START
        )
        future.result(5)
    writer.close()
    normalize_once(tmp_path)
    reader = Reader(tmp_path)
    markets = {row["market_id"]: row for row in reader.iter_rows("markets")}
    assert markets["polymarket:condition:question-1"]["family"] == "event_driven_question"
    assert markets["polymarket:condition:question-1"]["duration_s"] is None
    notes = list(reader.iter_rows("external_observations"))
    assert notes and notes[0]["payload"] == canonical({"text": "rain"})


@pytest.mark.asyncio
async def test_unclean_restart_records_an_uncertain_gap(tmp_path, monkeypatch):
    profile = replace(
        Profile(), storage=replace(Storage(), min_free_disk_gib=0, raw_fsync_interval_ms=1)
    )
    collector = Collector(tmp_path, profile, None, normalize_live=False)
    atomic_json(
        collector.collection_dir / "sessions" / "previous.json",
        {"session_id": "previous", "clean_stop": False, "started_at": 1},
    )

    async def idle(*_args, **_kwargs):
        await collector.stop.wait()

    async def no_op():
        return None

    monkeypatch.setattr("tyrex_pm.data_warehouse.supervisor.record_stream", idle)
    for owner, attr in (
        (collector.discovery, "cycle"),
        (collector.discovery, "scan"),
        (collector, "metadata_cycle"),
        (collector, "book_audit"),
        (collector, "resolution_cycle"),
        (collector, "clocks"),
    ):
        monkeypatch.setattr(owner, attr, no_op)
    await collector.run(0.05)
    normalize_once(tmp_path)
    events = Reader(tmp_path).iter_rows("operational_events")
    assert any(row["event_type"] == "session_interrupted" for row in events)
