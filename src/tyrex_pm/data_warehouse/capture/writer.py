"""Bounded intake with acknowledged fsync, immutable segments, and recovery."""

from __future__ import annotations

import concurrent.futures
import os
import queue
import shutil
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..config import Storage
from ..contracts import RawEnvelope, canonical, digest
from .files import Ownership, atomic_bytes, atomic_json, read_json, within


class DurabilityError(RuntimeError):
    pass


def segment_records(root: Path, manifest: dict[str, Any]) -> Iterator[RawEnvelope]:
    import zstandard

    physical = within(root, manifest["path"]).read_bytes()
    if digest(physical) != manifest["compressed_sha256"]:
        raise DurabilityError("compressed segment checksum mismatch")
    raw = zstandard.ZstdDecompressor().decompress(
        physical, max_output_size=manifest["uncompressed_bytes"]
    )
    if len(raw) != manifest["uncompressed_bytes"] or digest(raw) != manifest["sha256"]:
        raise DurabilityError("uncompressed segment checksum mismatch")
    import json

    count = 0
    for line in raw.splitlines():
        record = RawEnvelope(**json.loads(line))
        record.bytes()
        count += 1
        yield record
    if count != manifest["record_count"]:
        raise DurabilityError("segment row count mismatch")


def archive_bytes(root: Path) -> int:
    total = sum(int(manifest.get("uncompressed_bytes") or 0) for manifest in raw_manifests(root))
    spool = root / "spool"
    if spool.exists():
        total += sum(path.stat().st_size for path in spool.rglob("*.open.jsonl"))
    return total


def raw_manifests(root: Path, collection: str | None = None) -> list[dict[str, Any]]:
    result = []
    for path in sorted((root / "raw").rglob("*.manifest.json")):
        value = read_json(path)
        if collection is None or value["collection_id"] == collection:
            result.append(value)
    return sorted(result, key=lambda m: (m["first_received_at"], m["segment_id"]))


def seal(root: Path, path: Path, *, profile_hash: str, recovered: bool = False) -> dict:
    import json

    import zstandard

    raw = path.read_bytes()
    if not raw or not raw.endswith(b"\n"):
        raise DurabilityError("cannot seal empty or partial segment")
    rows = [RawEnvelope(**json.loads(line)) for line in raw.splitlines()]
    for row in rows:
        row.bytes()
    first = rows[0]
    day = datetime.fromtimestamp(first.received_at // 1_000_000_000, tz=timezone.utc)
    segment = path.name.removesuffix(".open.jsonl")
    folder = root / "raw" / f"source={first.source_id}" / day.strftime("date=%Y-%m-%d/hour=%H")
    target = folder / f"{segment}.jsonl.zst"
    compressed = zstandard.ZstdCompressor(level=3).compress(raw)
    if zstandard.ZstdDecompressor().decompress(compressed) != raw:
        raise DurabilityError("compression verification failed")
    manifest = {
        "manifest_version": 1,
        "segment_id": segment,
        "collection_id": first.collection_id,
        "session_id": first.session_id,
        "source_id": first.source_id,
        "profile_hash": profile_hash,
        "path": target.relative_to(root).as_posix(),
        "codec": "zstd",
        "compressed_bytes": len(compressed),
        "uncompressed_bytes": len(raw),
        "compressed_sha256": digest(compressed),
        "sha256": digest(raw),
        "record_count": len(rows),
        "first_received_at": first.received_at,
        "last_received_at": rows[-1].received_at,
        "sequences": [r.ingest_sequence for r in rows],
        "recovered": recovered,
        "sealed_at": time.time_ns(),
    }
    atomic_bytes(target, compressed)
    atomic_json(folder / f"{segment}.manifest.json", manifest)
    path.unlink()  # Only after verified representation and manifest publication.
    return manifest


def recover(root: Path) -> list[dict[str, Any]]:
    """Caller holds writer lock. Salvage verified complete lines; retain damaged originals."""
    import json

    existing = {m["segment_id"]: m for m in raw_manifests(root)}
    reports = []
    for path in sorted((root / "spool").rglob("*.open.jsonl")):
        segment = path.name.removesuffix(".open.jsonl")
        if segment in existing:
            list(segment_records(root, existing[segment]))
            path.unlink()
            continue
        data = path.read_bytes()
        good = []
        seen = set()
        rejected = 0
        for line in data.splitlines(keepends=True):
            try:
                if not line.endswith(b"\n"):
                    raise ValueError("torn line")
                row = RawEnvelope(**json.loads(line))
                row.bytes()
                if row.raw_record_id in seen:
                    raise ValueError("duplicate identity")
                seen.add(row.raw_record_id)
                good.append(line)
            except (ValueError, TypeError, KeyError):
                rejected += 1
        if rejected:
            quarantine = root / "quarantine" / f"{segment}.damaged.jsonl"
            atomic_bytes(quarantine, data)
            atomic_json(
                quarantine.with_suffix(".report.json"),
                {
                    "reason": "damaged_raw_segment",
                    "rejected_lines": rejected,
                    "lost_tail_duration": None,
                    "original_sha256": digest(data),
                },
            )
            if good:
                atomic_bytes(path, b"".join(good))
        if good:
            collection = RawEnvelope(**json.loads(good[0])).collection_id
            config = read_json(within(root, f"collections/{collection}/collection.json"))
            seal(root, path, profile_hash=config["profile_hash"], recovered=True)
        else:
            path.unlink()
        reports.append(
            {
                "segment_id": segment,
                "salvaged_records": len(good),
                "rejected_lines": rejected,
                "unclean_recovery": True,
            }
        )
    return reports


class RawWriter:
    """One producer event-loop, one writer thread. Futures complete after fsync."""

    def __init__(
        self, root: Path, collection: str, session: str, profile_hash: str, storage: Storage
    ):
        self.root = root.resolve()
        self.collection, self.session, self.profile_hash = collection, session, profile_hash
        self.storage = storage
        self.ownership = Ownership(self.root, "collector")
        try:
            self.recovery = recover(self.root)
        except BaseException:
            self.ownership.close()
            raise
        self.queue: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.sequence = 0
        self.queued_bytes = 0
        self.received = self.appended = self.durable = 0
        self.fault: BaseException | None = None
        self.closed = False
        self.last_durable_at: int | None = None
        self.max_queue_bytes = 0
        self.oldest_unflushed_at: int | None = None
        self.fsync_samples_ns: list[int] = []
        self.seal_queue: queue.Queue = queue.Queue(maxsize=64)
        self.thread = threading.Thread(target=self._run, name="warehouse-writer", daemon=True)
        self.seal_thread = threading.Thread(
            target=self._seal_loop, name="warehouse-sealer", daemon=True
        )
        self.thread.start()
        self.seal_thread.start()

    def _seal_loop(self) -> None:
        while True:
            path = self.seal_queue.get()
            if path is None:
                return
            try:
                seal(self.root, path, profile_hash=self.profile_hash)
            except BaseException as exc:
                with self.lock:
                    if isinstance(exc, DurabilityError):
                        fault = exc
                    else:
                        fault = DurabilityError("segment seal failed")
                    self.fault = fault

    def submit(
        self, body: str | bytes, *, source: str, stream: str, **kwargs: Any
    ) -> tuple[RawEnvelope, concurrent.futures.Future]:
        if not source.replace("_", "").replace("-", "").isalnum():
            raise ValueError("invalid source path identifier")
        payload_bytes = len(body.encode() if isinstance(body, str) else body)
        reservation = payload_bytes * 2 + 4096  # Account for envelope/encoding overhead.
        with self.lock:
            if self.fault or self.closed:
                raise DurabilityError("capture writer unavailable") from self.fault
            if self.queued_bytes + reservation > self.storage.capture_queue_mib * 1024**2:
                self.fault = DurabilityError("capture queue overflow; recording interrupted")
                raise self.fault
            self.sequence += 1
            record = RawEnvelope.create(
                body=body,
                collection_id=self.collection,
                session_id=self.session,
                sequence=self.sequence,
                source=source,
                stream=stream,
                **kwargs,
            )
            future: concurrent.futures.Future = concurrent.futures.Future()
            self.queued_bytes += reservation
            self.max_queue_bytes = max(self.max_queue_bytes, self.queued_bytes)
            self.received = self.sequence
            self.queue.put((record, future, reservation))
            return record, future

    def _run(self) -> None:
        handles: dict[str, Any] = {}
        paths: dict[str, Path] = {}
        opened: dict[str, float] = {}
        pending = []
        last_flush = time.monotonic()
        try:
            while True:
                interval = self.storage.raw_fsync_interval_ms / 1000
                timeout = max(0.001, interval - (time.monotonic() - last_flush))
                try:
                    item = self.queue.get(timeout=timeout)
                except queue.Empty:
                    item = "flush"
                if isinstance(item, tuple):
                    record, future, reservation = item
                    pending.append(item)
                    with self.lock:
                        if self.oldest_unflushed_at is None:
                            self.oldest_unflushed_at = record.received_at
                    if shutil.disk_usage(self.root).free < self.storage.min_free_disk_gib * 1024**3:
                        future.set_exception(DurabilityError("free disk below configured floor"))
                        raise DurabilityError("free disk below configured floor")
                    source = record.source_id
                    if source not in handles:
                        path = self.root / "spool" / self.session / f"{uuid.uuid4().hex}.open.jsonl"
                        path.parent.mkdir(parents=True, exist_ok=True)
                        handles[source] = path.open("xb")
                        paths[source], opened[source] = path, time.monotonic()
                    handles[source].write((canonical(asdict(record)) + "\n").encode())
                    self.appended = record.ingest_sequence
                flush = item is None or time.monotonic() - last_flush >= interval
                if flush:
                    for handle in handles.values():
                        handle.flush()
                        os.fsync(handle.fileno())
                    self.last_durable_at = time.time_ns()
                    for record, future, reservation in pending:
                        self.durable = record.ingest_sequence
                        with self.lock:
                            self.queued_bytes -= reservation
                        if not future.done():
                            future.set_result(record)
                        self.fsync_samples_ns.append(
                            time.monotonic_ns() - record.received_monotonic_ns
                        )
                    self.fsync_samples_ns = self.fsync_samples_ns[-10000:]
                    pending.clear()
                    with self.lock:
                        self.oldest_unflushed_at = None
                    last_flush = time.monotonic()
                    for source in list(handles):
                        if (
                            item is None
                            or time.monotonic() - opened[source] >= self.storage.raw_rotate_s
                            or handles[source].tell() >= self.storage.raw_rotate_mib * 1024**2
                        ):
                            handles.pop(source).close()
                            try:
                                self.seal_queue.put_nowait(paths.pop(source))
                            except queue.Full as exc:
                                raise DurabilityError(
                                    "sealing backlog exhausted; journal retained for recovery"
                                ) from exc
                            opened.pop(source)
                if item is None:
                    break
        except BaseException as exc:
            self.fault = exc
            for _, future, _ in pending:
                if not future.done():
                    future.set_exception(DurabilityError("durable write failed"))
            while not self.queue.empty():
                item = self.queue.get_nowait()
                if isinstance(item, tuple) and not item[1].done():
                    item[1].set_exception(DurabilityError("writer failed before acknowledgement"))
        finally:
            for handle in handles.values():
                handle.close()

    def close(self, timeout: float = 30) -> None:
        with self.lock:
            self.closed = True
            self.queue.put(None)
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise DurabilityError("writer drain timed out; ownership retained until process exit")
        try:
            self.seal_queue.put(None, timeout=timeout)
        except queue.Full as exc:
            raise DurabilityError(
                "sealer drain timed out; ownership retained until process exit"
            ) from exc
        self.seal_thread.join(timeout)
        if self.thread.is_alive() or self.seal_thread.is_alive():
            raise DurabilityError("writer drain timed out; ownership retained until process exit")
        self.ownership.close()
        if self.fault:
            raise DurabilityError("capture ended with storage failure") from self.fault

    def status(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "appended": self.appended,
            "fsynced": self.durable,
            "queued_bytes": self.queued_bytes,
            "max_queue_bytes": self.max_queue_bytes,
            "last_durable_at": self.last_durable_at,
            "oldest_unflushed_age_s": (
                None
                if self.oldest_unflushed_at is None
                else max(0, (time.time_ns() - self.oldest_unflushed_at) / 10**9)
            ),
            "writer_healthy": self.fault is None,
            "pending_seal_segments": self.seal_queue.qsize(),
        }
