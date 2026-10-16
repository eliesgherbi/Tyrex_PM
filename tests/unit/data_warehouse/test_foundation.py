from __future__ import annotations

import json
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest

from tyrex_pm.data_warehouse.capture.files import Ownership, atomic_json, read_json, within
from tyrex_pm.data_warehouse.capture.writer import (
    RawWriter,
    raw_manifests,
    recover,
    segment_records,
)
from tyrex_pm.data_warehouse.catalog.sqlite import Catalog
from tyrex_pm.data_warehouse.config import Profile, Storage, load_credentials, load_profile, strict
from tyrex_pm.data_warehouse.contracts import RawEnvelope, canonical, epoch_ns, exact
from tyrex_pm.data_warehouse.families.btc_5m import BtcFiveMinute
from tyrex_pm.data_warehouse.normalize.registry import normalize, register_external
from tyrex_pm.data_warehouse.normalize.worker import compact, normalize_once
from tyrex_pm.data_warehouse.query.export import export_market, load_export, verify
from tyrex_pm.data_warehouse.query.reader import Reader, exact_rows, sql_projection
from tyrex_pm.data_warehouse.reconstruction.books import Books
from tyrex_pm.data_warehouse.reconstruction.boundaries import resolution_assessment

START = 1780000200 * 10**9
MID = "polymarket:condition:fictional-condition"
UP = "polymarket:137:outcome:123456789012345678901234567890123456789012345678901234567890"
TOKEN = UP.rsplit(":", 1)[1]


def market():
    return {
        "id": "fixture",
        "slug": "btc-updown-5m-1780000200",
        "conditionId": "fictional-condition",
        "outcomes": ["Down", "Up"],
        "clobTokenIds": ["2", TOKEN],
        "description": "Synthetic rules, not official.",
    }


def raw(payload, source="clob", seq=1, at=START, connection="connection", **kwargs):
    return RawEnvelope.create(
        body=canonical(payload),
        collection_id="collection",
        session_id="session",
        sequence=seq,
        source=source,
        stream=source,
        connection=connection,
        received_at=at,
        **kwargs,
    )


def snapshot(**overrides):
    return {
        "event_type": "book",
        "asset_id": TOKEN,
        "market": "fictional-condition",
        "bids": [{"price": "0.49", "size": "150"}],
        "asks": [{"price": "0.52", "size": "90"}, {"price": "0.51", "size": "120"}],
        "timestamp": str(START // 10**6),
        **overrides,
    }


@pytest.fixture
def writer(tmp_path):
    profile = replace(
        Profile(), storage=replace(Storage(), raw_fsync_interval_ms=1, min_free_disk_gib=0)
    )
    atomic_json(
        tmp_path / "collections/collection/collection.json",
        {"profile": asdict(profile), "profile_hash": profile.hash},
    )
    recorder = RawWriter(tmp_path, "collection", "session", profile.hash, profile.storage)
    yield recorder
    if not recorder.closed:
        recorder.close()


def submit(writer, payload, source, at, **kwargs):
    row, future = writer.submit(
        canonical(payload),
        source=source,
        stream=source,
        connection="connection",
        received_at=at,
        **kwargs,
    )
    assert future.result(timeout=5) == row
    return row


def test_profiles_and_credentials_are_strict_and_private(tmp_path):
    assert load_profile() == load_profile(Path("config/data_collection/btc_5m.yaml"))
    with pytest.raises(ValueError, match="unknown"):
        strict(Profile, {"storag": {}})
    with pytest.raises(ValueError, match="requires int"):
        strict(Profile, {"schema_version": True})
    env = tmp_path / ".env"
    env.write_text(
        "POLYMARKET_API_KEY=fixture-key\nPOLYMARKET_API_SECRET='fixture-secret'\n"
        "POLYMARKET_API_PASSPHRASE=fixture-pass\nPRIVATE_KEY=never-load\n"
    )
    credentials = load_credentials(env, {})
    assert credentials.auth() == {
        "apiKey": "fixture-key",
        "secret": "fixture-secret",
        "passphrase": "fixture-pass",
    }
    assert "fixture-secret" not in repr(credentials)
    assert "never-load" not in canonical(credentials.auth())
    with pytest.raises(ValueError, match="complete TYREX"):
        load_credentials(env, {"TYREX_DATA_PM_API_KEY": "partial"})


@pytest.mark.parametrize(
    "value", [float("nan"), 0.5, "NaN", "1e100", "0.0000000000000000000000000000001"]
)
def test_reject_lossy_decimals(value):
    with pytest.raises(ValueError):
        exact(value)


def test_integer_time_and_label_mapping():
    assert epoch_ns("1780000200000000001", "ns") == START + 1
    assert epoch_ns(None) is None
    mapped = BtcFiveMinute().identify(market())
    assert mapped["instruments"][1]["normalized_label"] == "UP"
    assert mapped["instruments"][1]["token_id"] == TOKEN
    with pytest.raises(ValueError):
        BtcFiveMinute().identify({**market(), "endDate": "2026-01-01T00:00:00Z"})


def test_batch_reconstruction_and_connection_gap():
    state = Books()
    events = [
        raw(snapshot()),
        raw(
            {
                "event_type": "price_change",
                "market": "fictional-condition",
                "price_changes": [
                    {"asset_id": TOKEN, "side": "SELL", "price": "0.51", "size": "75"},
                    {"asset_id": TOKEN, "side": "SELL", "price": "0.52", "size": "0"},
                ],
            },
            seq=2,
        ),
    ]
    for event in events:
        for dataset, row in normalize(event):
            state.apply(dataset, row)
    assert state.books[UP].asks == {Decimal("0.51"): Decimal("75")}
    assert state.books[UP].valid
    gap = raw(
        {"type": "disconnected", "source": "clob", "connection_id": "connection"},
        "collector",
        seq=3,
        transport="collector_event",
    )
    for dataset, row in normalize(gap):
        state.apply(dataset, row)
    assert not state.books[UP].valid
    assert state.epochs[-1]["reason"] == "disconnected"
    audit = raw(snapshot(), seq=4, transport="http", http_status=200)
    for dataset, row in normalize(audit):
        state.apply(dataset, row)
    assert not state.books[UP].valid


def test_binance_bridge_and_sequence_break():
    state = Books()
    payloads = [
        {"e": "depthUpdate", "U": 101, "u": 102, "b": [["60000", "3"]], "a": []},
        {"lastUpdateId": 100, "bids": [["59999", "2"]], "asks": [["60001", "4"]]},
        {"e": "depthUpdate", "U": 105, "u": 106, "b": [], "a": []},
    ]
    for index, payload in enumerate(payloads):
        for dataset, row in normalize(raw(payload, "binance", seq=index + 1)):
            state.apply(dataset, row)
        if index == 1:
            assert state.books["binance:spot:BTCUSDT"].valid
            assert state.books["binance:spot:BTCUSDT"].bids[Decimal("60000")] == 3
    assert not state.books["binance:spot:BTCUSDT"].valid


def test_history_keeps_arrival_time_and_unknown_is_accounted():
    frame = raw(
        {
            "channel": "price.crypto.twap",
            "seq": 1,
            "snapshot": True,
            "payload": {
                "symbol": "btcusd",
                "source": "chainlink",
                "data": [
                    {"timestamp": START // 10**6 - 1000, "full_accuracy_value": "60000.123456789"}
                ],
            },
        },
        "polybolt",
    )
    ref = normalize(frame)[0][1]
    assert ref["source_time"] == START - 10**9
    assert ref["received_at"] == START
    assert ref["delivery_kind"] == "subscription_history"
    unknown = normalize(raw({"event_type": "new_future_event"}))
    assert unknown[0][1]["status"] == "UNKNOWN_TYPE"


def test_record_normalize_query_export_and_compact(writer, tmp_path):
    submit(writer, market(), "gamma", START - 1000)
    submit(writer, snapshot(), "clob", START)
    submit(
        writer,
        {
            "event_type": "price_change",
            "market": "fictional-condition",
            "price_changes": [{"asset_id": TOKEN, "side": "SELL", "price": "0.51", "size": "75"}],
        },
        "clob",
        START + 1,
    )
    writer.close()
    assert normalize_once(tmp_path)["new_builds"] == 2
    assert normalize_once(tmp_path)["new_builds"] == 0
    reader = Reader(tmp_path)
    assert reader.book_as_observed(UP, START)["asks"][0]["quantity"] == 120
    assert reader.book_as_observed(UP, START + 1)["asks"][0]["quantity"] == 75
    assert verify(tmp_path)["normalization_complete"]
    export_market(reader, MID, tmp_path / "export")
    assert load_export(tmp_path / "export")["labels_separate"]
    compacted = compact(tmp_path, "collection")
    assert compacted["compacted"] == 2
    checksums = read_json(
        tmp_path / "builds" / compacted["build_id"] / "manifest.json"
    )["logical_checksums"]
    assert checksums and all(checksums.values())
    assert len(list(Reader(tmp_path).iter_rows("book_snapshots"))) == 1
    assert reader.book_as_observed(UP, START)["asks"][0]["quantity"] == 120  # pinned pre-compaction


def test_exact_decimal256_nested_and_sql_projection():
    value = exact("1234567890123456789012345678901234567890123456.123456789012345678901234567890")
    table = pa.Table.from_pylist(
        [{"at": START + 1, "levels": [{"v": value}]}],
        schema=pa.schema(
            [
                ("at", pa.timestamp("ns", tz="UTC")),
                ("levels", pa.list_(pa.struct([("v", pa.decimal256(76, 30))]))),
            ]
        ),
    )
    assert exact_rows(table)[0]["at"] == START + 1
    projected = sql_projection(table)
    import duckdb

    connection = duckdb.connect()
    connection.register("data", projected)
    row = connection.execute("SELECT at_epoch_ns, levels[1].v FROM data").fetchone()
    assert row == (START + 1, str(value))
    connection.close()


def test_recovery_quarantines_torn_line_and_preserves_identity(tmp_path):
    atomic_json(tmp_path / "collections/collection/collection.json", {"profile_hash": "fixture"})
    spool = tmp_path / "spool/session/segment.open.jsonl"
    spool.parent.mkdir(parents=True)
    record = raw(snapshot())
    spool.write_bytes((canonical(asdict(record)) + "\n").encode() + b'{"torn":')
    with Ownership(tmp_path, "collector"):
        report = recover(tmp_path)
    assert report[0]["rejected_lines"] == 1
    restored = list(segment_records(tmp_path, raw_manifests(tmp_path)[0]))
    assert restored[0] == record
    assert list((tmp_path / "quarantine").glob("*.damaged.jsonl"))


def test_ownership_and_manifest_traversal(tmp_path):
    with Ownership(tmp_path, "collector"):
        with pytest.raises(ValueError, match="already owns"):
            Ownership(tmp_path, "collector")
    with pytest.raises(ValueError, match="escapes"):
        within(tmp_path, "../outside")


def test_projection_catalog_rebuild(writer, tmp_path):
    submit(
        writer,
        {
            "type": "projection",
            "namespace": "tasks",
            "key": "pending",
            "value": {"status": "PENDING"},
        },
        "collector",
        START,
        transport="collector_event",
    )
    writer.close()
    catalog = Catalog(tmp_path)
    catalog.rebuild()
    assert catalog.values("collection", "tasks") == {"pending": {"status": "PENDING"}}
    catalog.rebuild()
    assert catalog.db.execute("SELECT COUNT(*) FROM applied").fetchone()[0] == 1
    catalog.close()


def test_non_price_extension_and_fractional_resolution():
    register_external("fictional_weather_test", "1", lambda payload: None)
    rows = normalize(
        raw(
            {
                "feed_id": "fixture-weather",
                "subject_id": "fixture-location",
                "observation_type": "weather",
                "payload_schema_id": "fictional_weather_test",
                "payload_schema_version": "1",
                "payload": {"description": "rain"},
            },
            "external",
        )
    )
    assert json.loads(rows[0][1]["payload"]) == {"description": "rain"}
    rows = [
        {
            "record_id": "r",
            "received_at": START,
            "provider_status": "TEST_TERMINAL",
            "payouts": [Decimal("0.5"), Decimal("0.5")],
        }
    ]
    assert resolution_assessment(rows)["status"] == "UNSUPPORTED"
    assert (
        resolution_assessment(
            rows, supported_statuses=("TEST_TERMINAL",), payout_unit="USD_per_share"
        )["payouts"]
        == [Decimal("0.5")] * 2
    )
