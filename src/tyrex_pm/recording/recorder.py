"""Bounded, non-blocking recorder: capture -> queue -> background serialize/write.

Capture (trading loop): ``capture(stream, record)`` stores an already-built, fresh
mapping of decision-time primitives with its capture time and an estimated size. It never
serializes, never does I/O and never blocks; on overflow the record is dropped (or the
oldest is, per policy), the drop is counted and a recording gap interval is extended.

Writer (one background thread): drains batches, serializes JSON lines, writes zstd
segments, rotates by size or age and writes one manifest per closed segment. Serialization
competes for the GIL; its CPU time is measured and reported (``writer_cpu_s``).

Failure exposure is independent of the writer: counters live in this object and are read
by the trading loop (``status()``), which reports them through run evidence and the run
summary. A writer exception marks the stream failed; later captures are counted as
dropped. ``close(timeout_s)`` stops intake, drains up to the timeout and counts what is
left as ``dropped_at_shutdown``.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from tyrex_pm.recording.config import RecordingConfig

RECORDER_VERSION = "tyrex_recording_v1"
_MAX_GAPS = 100
_LATENCY_SAMPLES = 4096
_BATCH = 2000
# Queue accounting uses a per-stream size hint (O(1) per capture): seeded by one
# structural estimate, re-estimated exactly every Nth capture, and raised by the writer
# from actual serialized sizes x the in-memory overhead of Python containers.
_RESAMPLE_EVERY = 256
_MEMORY_PER_JSON_BYTE = 3
# The writer drains on a timer and is woken early only when a stream's queue is this
# long: waking it per capture made it compete for the GIL on every evaluation.
_DRAIN_INTERVAL_S = 0.25
_WAKE_AT_RECORDS = 512


def estimate_size(value: Any, _depth: int = 0) -> int:
    """Structural size estimate in bytes (queue accounting, not exact memory).

    Walks the record (about 90 us for a decision row), so capture calls it only to seed
    and periodically refresh the stream's size hint, never per record.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return 24
    if isinstance(value, str):
        return 49 + len(value)
    if _depth > 6:
        return 64
    if isinstance(value, Mapping):
        return 64 + sum(estimate_size(k, _depth + 1) + estimate_size(v, _depth + 1)
                        for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return 56 + sum(estimate_size(v, _depth + 1) for v in value)
    return 64


@dataclass
class StreamStatus:
    name: str
    schema: str
    state: str = "running"  # running | failed | closed
    captured: int = 0
    written: int = 0
    dropped_overflow: int = 0
    dropped_after_failure: int = 0
    dropped_at_shutdown: int = 0
    serialize_failed: int = 0
    # Taken by the writer, not yet written. At close, what the writer still holds after
    # the timeout is ``unconfirmed_at_shutdown``; records in a segment that could not be
    # closed after a failure are ``lost_in_failed_segment`` (moved out of ``written``).
    in_flight: int = 0
    unconfirmed_at_shutdown: int = 0
    lost_in_failed_segment: int = 0
    queue_records: int = 0
    queue_bytes: int = 0
    queue_high_records: int = 0
    queue_high_bytes: int = 0
    bytes_uncompressed: int = 0
    segments: int = 0
    write_latency_ms_p50: float | None = None
    write_latency_ms_p99: float | None = None
    last_error: str | None = None
    gaps: list[dict] = field(default_factory=list)
    gaps_truncated: int = 0

    @property
    def dropped(self) -> int:
        return (self.dropped_overflow + self.dropped_after_failure
                + self.dropped_at_shutdown + self.serialize_failed
                + self.unconfirmed_at_shutdown + self.lost_in_failed_segment)


class _Segment:
    def __init__(self, directory: Path, stream: str, index: int, schema: str) -> None:
        import zstandard

        self.stream, self.schema = stream, schema
        self.path = directory / f"part-{index:06d}.jsonl.zst"
        self._raw = self.path.open("wb")
        self._writer = zstandard.ZstdCompressor(level=3).stream_writer(self._raw)
        self.opened_mono = time.monotonic()
        self.records = 0
        self.bytes_uncompressed = 0
        self.first_capture_ns: int | None = None
        self.last_capture_ns: int | None = None

    def write(self, line: bytes, capture_ns: int) -> None:
        self._writer.write(line)
        self.records += 1
        self.bytes_uncompressed += len(line)
        if self.first_capture_ns is None:
            self.first_capture_ns = capture_ns
        self.last_capture_ns = capture_ns

    def close(self) -> dict:
        self._writer.flush()
        self._writer.close()  # also closes the file
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        manifest = {
            "recorder": RECORDER_VERSION, "stream": self.stream, "schema": self.schema,
            "file": self.path.name, "compression": "zstd", "format": "jsonl",
            "records": self.records, "bytes_uncompressed": self.bytes_uncompressed,
            "first_capture_ns": self.first_capture_ns,
            "last_capture_ns": self.last_capture_ns, "sha256": digest,
        }
        self.path.with_suffix("").with_suffix(".manifest.json").write_text(
            json.dumps(manifest, indent=1), encoding="utf-8")
        return manifest


class _Stream:
    def __init__(self, name: str, schema: str, config: RecordingConfig, directory: Path):
        self.status = StreamStatus(name=name, schema=schema)
        self.config = config
        self.directory = directory
        self.lock = threading.Lock()
        self.queue: deque[tuple[int, int, dict]] = deque()
        self.segment: _Segment | None = None
        self.segment_index = 0
        self.latencies: deque[float] = deque(maxlen=_LATENCY_SAMPLES)
        self.accepting = True
        self.size_hint = 0  # estimated bytes per queued record (see _RESAMPLE_EVERY)

    def _note_gap(self, capture_ns: int, cause: str) -> None:
        gaps = self.status.gaps
        if gaps and gaps[-1]["cause"] == cause and capture_ns - gaps[-1]["end_ns"] <= 1e9:
            gaps[-1]["end_ns"] = capture_ns
            gaps[-1]["records"] += 1
            return
        if len(gaps) >= _MAX_GAPS:
            self.status.gaps_truncated += 1
            return
        gaps.append({"start_ns": capture_ns, "end_ns": capture_ns, "records": 1,
                     "cause": cause})

    def capture(self, record: dict, capture_ns: int) -> bool:
        st = self.status
        if not self.size_hint or st.captured % _RESAMPLE_EVERY == 0:
            self.size_hint = max(self.size_hint, estimate_size(record))
        size = self.size_hint
        with self.lock:
            st.captured += 1
            if not self.accepting or st.state != "running":
                if st.state == "failed":
                    st.dropped_after_failure += 1
                    self._note_gap(capture_ns, "writer_failed")
                else:
                    st.dropped_at_shutdown += 1
                    self._note_gap(capture_ns, "closed")
                return False
            full = (len(self.queue) >= self.config.queue_max_records
                    or st.queue_bytes + size > self.config.queue_max_bytes)
            if full and self.config.overflow == "drop_oldest" and self.queue:
                old_ns, old_size, _ = self.queue.popleft()
                st.queue_bytes -= old_size
                st.dropped_overflow += 1
                self._note_gap(old_ns, "overflow")
            elif full:
                st.dropped_overflow += 1
                self._note_gap(capture_ns, "overflow")
                return False
            self.queue.append((capture_ns, size, record))
            st.queue_bytes += size
            st.queue_records = len(self.queue)
            st.queue_high_records = max(st.queue_high_records, st.queue_records)
            st.queue_high_bytes = max(st.queue_high_bytes, st.queue_bytes)
            return True

    def take(self, limit: int) -> list[tuple[int, int, dict]]:
        with self.lock:
            batch = []
            while self.queue and len(batch) < limit:
                item = self.queue.popleft()
                self.status.queue_bytes -= item[1]
                batch.append(item)
            self.status.queue_records = len(self.queue)
            self.status.in_flight += len(batch)
            return batch

    def write(self, batch: list[tuple[int, int, dict]]) -> None:
        if self.status.state == "failed":
            self._fail_rest(batch)
            return
        done = 0
        try:
            for capture_ns, _size, record in batch:
                try:
                    line = (json.dumps(record, separators=(",", ":"), default=str)
                            + "\n").encode("utf-8")
                except (TypeError, ValueError):
                    with self.lock:
                        self.status.serialize_failed += 1
                        self.status.in_flight -= 1
                        self._note_gap(capture_ns, "serialize_failed")
                    done += 1
                    continue
                if self.segment is None:
                    self._open_segment()
                self.segment.write(line, capture_ns)
                with self.lock:
                    self.status.written += 1
                    self.status.in_flight -= 1
                    self.status.bytes_uncompressed += len(line)
                    observed = len(line) * _MEMORY_PER_JSON_BYTE
                    if observed > self.size_hint:
                        self.size_hint = observed
                done += 1
                self.latencies.append((time.time_ns() - capture_ns) / 1e6)
                if self._segment_due():
                    self._close_segment()
        except Exception as exc:  # noqa: BLE001 - recording never stops trading
            with self.lock:
                self.status.state = "failed"
                self.status.last_error = f"{type(exc).__name__}: {exc}"
            self._fail_rest(batch[done:])
            self._abandon_segment()

    def _fail_rest(self, items: list[tuple[int, int, dict]]) -> None:
        with self.lock:
            for capture_ns, _size, _record in items:
                self.status.dropped_after_failure += 1
                self.status.in_flight -= 1
                self._note_gap(capture_ns, "writer_failed")

    def _segment_due(self) -> bool:
        seg = self.segment
        return seg is not None and (
            seg.bytes_uncompressed >= self.config.segment_max_bytes
            or time.monotonic() - seg.opened_mono >= self.config.segment_max_seconds)

    def _open_segment(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.segment_index += 1
        self.segment = _Segment(self.directory, self.status.name, self.segment_index,
                                self.status.schema)

    def _close_segment(self) -> None:
        if self.segment is None:
            return
        segment, self.segment = self.segment, None
        try:
            segment.close()
        except Exception:
            with self.lock:  # its records are not durable: move them out of written
                self.status.written -= segment.records
                self.status.lost_in_failed_segment += segment.records
            raise
        with self.lock:
            self.status.segments += 1

    def _abandon_segment(self) -> None:
        segment, self.segment = self.segment, None
        if segment is not None:
            try:
                segment.close()
            except Exception:  # noqa: BLE001 - its records are not durable: count them
                with self.lock:
                    self.status.written -= segment.records
                    self.status.lost_in_failed_segment += segment.records

    def rotate_if_due(self) -> None:
        if self.status.state != "failed" and self._segment_due():
            try:
                self._close_segment()
            except Exception as exc:  # noqa: BLE001
                with self.lock:
                    self.status.state = "failed"
                    self.status.last_error = f"{type(exc).__name__}: {exc}"

    def snapshot(self) -> dict:
        with self.lock:
            out = asdict(self.status)
            out["gaps"] = [dict(g) for g in self.status.gaps]
        lat = sorted(self.latencies)
        if lat:
            out["write_latency_ms_p50"] = round(lat[len(lat) // 2], 3)
            out["write_latency_ms_p99"] = round(lat[min(len(lat) - 1, int(.99 * len(lat)))], 3)
        out["dropped"] = self.status.dropped
        return out


class Recorder:
    """Streams of one run (e.g. ``decisions``, ``explain``) and their single writer."""

    def __init__(self, directory: Path, config: RecordingConfig, *,
                 streams: Mapping[str, str], manifest: Mapping[str, Any]) -> None:
        self.directory = Path(directory)
        self.config = config
        self.manifest = {"recorder": RECORDER_VERSION, **dict(manifest),
                         "streams": dict(streams), "config": config.identity()}
        self._streams = {name: _Stream(name, schema, config, self.directory / name)
                         for name, schema in streams.items()}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._writer_cpu_s = 0.0
        self._writer_error: str | None = None
        self.closed_status: dict | None = None

    # -- lifecycle -------------------------------------------------------------------
    def start(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "manifest.json").write_text(
            json.dumps(self.manifest, indent=1, default=str), encoding="utf-8")
        self._thread = threading.Thread(target=self._run, name="tyrex-recording",
                                        daemon=True)
        self._thread.start()

    def close(self, timeout_s: float | None = None) -> dict:
        """Stop intake, drain up to ``timeout_s``, count the rest, write status.json."""
        if self.closed_status is not None:
            return self.closed_status
        timeout = self.config.shutdown_timeout_s if timeout_s is None else timeout_s
        for stream in self._streams.values():
            with stream.lock:
                stream.accepting = False
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout))
        writer_stuck = self._thread is not None and self._thread.is_alive()
        for stream in self._streams.values():
            with stream.lock:
                if writer_stuck and stream.status.in_flight:
                    # Held by a writer that missed the deadline: not confirmed durable.
                    stream.status.unconfirmed_at_shutdown += stream.status.in_flight
                    stream.status.in_flight = 0
                left = len(stream.queue)
                for capture_ns, _size, _record in stream.queue:
                    stream._note_gap(capture_ns, "shutdown_timeout")
                stream.queue.clear()
                stream.status.queue_records = 0
                stream.status.queue_bytes = 0
                stream.status.dropped_at_shutdown += left
                if stream.status.state == "running":
                    stream.status.state = "closed"
        status = self.status()
        status["writer_finished"] = self._thread is None or not self._thread.is_alive()
        try:
            (self.directory / "status.json").write_text(
                json.dumps(status, indent=1, default=str), encoding="utf-8")
        except OSError as exc:
            status["status_write_error"] = f"{type(exc).__name__}: {exc}"
        self.closed_status = status
        return status

    # -- capture (trading loop) ------------------------------------------------------
    def capture(self, stream: str, record: dict, *, capture_ns: int | None = None) -> bool:
        target = self._streams.get(stream)
        if target is None:
            return False
        accepted = target.capture(record, time.time_ns() if capture_ns is None else capture_ns)
        if accepted and len(target.queue) >= _WAKE_AT_RECORDS:
            self._wake.set()
        return accepted

    def has_stream(self, stream: str) -> bool:
        return stream in self._streams

    # -- status (read by the trading loop; independent of the writer) -----------------
    def status(self) -> dict:
        alive = self._thread is not None and self._thread.is_alive()
        streams = {name: s.snapshot() for name, s in self._streams.items()}
        if self._thread is not None and not alive and not self._stop.is_set():
            for snap in streams.values():
                snap["state"] = "failed"
                snap["last_error"] = snap["last_error"] or self._writer_error or "writer died"
        return {
            "recorder": RECORDER_VERSION,
            "directory": str(self.directory),
            "writer_alive": alive,
            "writer_cpu_s": round(self._writer_cpu_s, 4),
            "writer_error": self._writer_error,
            "streams": streams,
            "dropped": sum(s["dropped"] for s in streams.values()),
            "failed": any(s["state"] == "failed" for s in streams.values()),
        }

    # -- writer thread ---------------------------------------------------------------
    def _run(self) -> None:
        try:
            while True:
                self._wake.wait(timeout=_DRAIN_INTERVAL_S)
                self._wake.clear()
                progressed = False
                for stream in self._streams.values():
                    batch = stream.take(_BATCH)
                    if batch:
                        stream.write(batch)
                        progressed = True
                    stream.rotate_if_due()
                self._writer_cpu_s = time.thread_time()
                if self._stop.is_set() and not progressed and not any(
                        s.queue for s in self._streams.values()):
                    break
                if progressed:
                    self._wake.set()  # keep draining without waiting
        except BaseException as exc:  # noqa: BLE001 - surfaced through status()
            self._writer_error = f"{type(exc).__name__}: {exc}"
        finally:
            for stream in self._streams.values():
                try:
                    stream._close_segment()
                except Exception as exc:  # noqa: BLE001
                    with stream.lock:
                        stream.status.state = "failed"
                        stream.status.last_error = f"{type(exc).__name__}: {exc}"
            self._writer_cpu_s = time.thread_time()
