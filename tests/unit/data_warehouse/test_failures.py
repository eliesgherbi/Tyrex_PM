from __future__ import annotations

import subprocess
import sys
from dataclasses import asdict, replace

import pytest

from tyrex_pm.data_warehouse.capture.files import atomic_json
from tyrex_pm.data_warehouse.capture.writer import (
    DurabilityError,
    RawWriter,
    raw_manifests,
    recover,
    segment_records,
)
from tyrex_pm.data_warehouse.config import Profile, Storage
from tyrex_pm.data_warehouse.contracts import canonical, epoch_ns
from tyrex_pm.data_warehouse.normalize import worker
from tyrex_pm.data_warehouse.query.export import verify
from tyrex_pm.data_warehouse.query.reader import Reader


def create_writer(root):
    storage = replace(Storage(), raw_fsync_interval_ms=1, min_free_disk_gib=0, capture_queue_mib=1)
    profile = replace(Profile(), storage=storage)
    atomic_json(
        root / "collections/c/collection.json",
        {"profile": asdict(profile), "profile_hash": profile.hash},
    )
    return RawWriter(root, "c", "s", profile.hash, storage)


def write_trade(writer):
    raw, future = writer.submit(
        canonical({"e": "trade", "t": 1, "p": "60000", "q": "1", "m": True, "T": 1}),
        source="binance",
        stream="trades",
    )
    future.result(5)
    return raw


def test_process_death_after_fsync_preserves_acknowledged_record(tmp_path):
    program = """
import os,sys
from pathlib import Path
from dataclasses import asdict,replace
from tyrex_pm.data_warehouse.config import Profile,Storage
from tyrex_pm.data_warehouse.capture.files import atomic_json
from tyrex_pm.data_warehouse.capture.writer import RawWriter
root=Path(sys.argv[1])
profile=replace(Profile(),storage=replace(
 Storage(),min_free_disk_gib=0,raw_fsync_interval_ms=1))
atomic_json(root/'collections/c/collection.json',{
 'profile':asdict(profile),'profile_hash':profile.hash})
writer=RawWriter(root,'c','s',profile.hash,profile.storage)
raw,future=writer.submit('PONG',source='clob',stream='test')
future.result(5)
os._exit(9)
"""
    process = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)], capture_output=True, timeout=15, check=False
    )
    assert process.returncode == 9, process.stderr
    recovered = recover(tmp_path)
    assert recovered[0]["salvaged_records"] == 1
    rows = list(segment_records(tmp_path, raw_manifests(tmp_path)[0]))
    assert rows[0].raw_record_id == "s:1"


def test_disk_floor_rejects_the_unwritten_record(tmp_path, monkeypatch):
    storage = replace(Storage(), raw_fsync_interval_ms=1, min_free_disk_gib=1, capture_queue_mib=1)
    profile = replace(Profile(), storage=storage)
    atomic_json(
        tmp_path / "collections/c/collection.json",
        {"profile": asdict(profile), "profile_hash": profile.hash},
    )
    usage = type("Usage", (), {"total": 10, "used": 10, "free": 0})()
    monkeypatch.setattr(
        "tyrex_pm.data_warehouse.capture.writer.shutil.disk_usage", lambda _path: usage
    )
    writer = RawWriter(tmp_path, "c", "s", profile.hash, storage)
    _raw, future = writer.submit("PONG", source="clob", stream="test")
    with pytest.raises(DurabilityError, match="free disk"):
        future.result(5)
    with pytest.raises(DurabilityError):
        writer.close()
    assert writer.durable == 0


def test_overflow_is_a_fatal_visible_fault(tmp_path):
    writer = create_writer(tmp_path)
    with pytest.raises(DurabilityError, match="overflow"):
        writer.submit("x" * 600000, source="clob", stream="test")
    with pytest.raises(DurabilityError):
        writer.close()
    assert writer.status()["writer_healthy"] is False


def test_fsync_failure_never_acknowledges(tmp_path, monkeypatch):
    writer = create_writer(tmp_path)

    def fail(_):
        raise OSError("injected storage fault")

    monkeypatch.setattr("tyrex_pm.data_warehouse.capture.writer.os.fsync", fail)
    _, future = writer.submit("PONG", source="clob", stream="test")
    with pytest.raises(DurabilityError):
        future.result(5)
    with pytest.raises(DurabilityError):
        writer.close()
    assert writer.durable == 0


@pytest.mark.parametrize("after_publish", [False, True])
def test_publication_crash_recovers_without_duplicate_rows(tmp_path, monkeypatch, after_publish):
    writer = create_writer(tmp_path)
    write_trade(writer)
    writer.close()
    original = worker.atomic_json

    def interrupted(path, value):
        if path.name == "manifest.json":
            if after_publish:
                original(path, value)
            raise OSError("injected publication crash")
        return original(path, value)

    monkeypatch.setattr(worker, "atomic_json", interrupted)
    with pytest.raises(OSError):
        worker.normalize_once(tmp_path)
    assert len(list(Reader(tmp_path).iter_rows("trade_prints"))) == int(after_publish)
    monkeypatch.setattr(worker, "atomic_json", original)
    worker.normalize_once(tmp_path)
    assert len(list(Reader(tmp_path).iter_rows("trade_prints"))) == 1
    assert verify(tmp_path)["normalization_complete"]


def test_archive_corruption_fails_integrity(tmp_path):
    writer = create_writer(tmp_path)
    write_trade(writer)
    writer.close()
    manifest = raw_manifests(tmp_path)[0]
    (tmp_path / manifest["path"]).write_bytes(b"corrupt")
    result = verify(tmp_path)
    assert not result["ok"] and not result["integrity_ok"]


def test_iso_nanoseconds_are_not_truncated():
    assert epoch_ns("2026-01-01T00:00:00.123456789Z", "iso") % 10**9 == 123456789
    assert epoch_ns("2026-01-01T01:00:00.123456789+01:00", "iso") == epoch_ns(
        "2026-01-01T00:00:00.123456789Z", "iso"
    )
