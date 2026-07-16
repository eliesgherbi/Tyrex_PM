"""Non-blocking MarketEvent recorder sink (Phase 2B M2B.1-A / M2B.1-B).

``emit()`` is O(1) queue enqueue only — disk I/O runs in a background asyncio task.
Do not use :class:`~tyrex_pm.reporting.sinks.jsonl.JsonlSink` for event recording.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from tyrex_pm.core.errors import ConfigError
from tyrex_pm.core.events import EventType, MarketEvent, event_to_dict

log = logging.getLogger(__name__)

MANIFEST_SCHEMA_VERSION = 1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso8601(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def _require_zstandard():
    try:
        import zstandard as zstd  # noqa: PLC0415
    except ImportError as exc:
        raise ConfigError(
            "recording.compress=true requires optional dependency zstandard; "
            "install with: pip install 'tyrex-pm[record]'"
        ) from exc
    return zstd


@dataclass
class SegmentStats:
    path: str
    event_count: int = 0
    first_recv_ts: str | None = None
    last_recv_ts: str | None = None
    bytes: int = 0


@dataclass
class ManifestState:
    schema_version: int = MANIFEST_SCHEMA_VERSION
    market_id: str = ""
    yes_token_id: str | None = None
    no_token_id: str | None = None
    recording_started_ts: str | None = None
    recording_ended_ts: str | None = None
    segments: list[SegmentStats] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    dropped_events: int = 0
    event_types_seen: dict[str, int] = field(default_factory=dict)
    git_sha: str = "unknown"
    scenario: str = ""
    linked_run_ids: list[str] = field(default_factory=list)
    compressed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "market_id": self.market_id,
            "yes_token_id": self.yes_token_id,
            "no_token_id": self.no_token_id,
            "recording_started_ts": self.recording_started_ts,
            "recording_ended_ts": self.recording_ended_ts,
            "segments": [
                {
                    "path": s.path,
                    "event_count": s.event_count,
                    "first_recv_ts": s.first_recv_ts,
                    "last_recv_ts": s.last_recv_ts,
                    "bytes": s.bytes,
                }
                for s in self.segments
            ],
            "gaps": list(self.gaps),
            "dropped_events": self.dropped_events,
            "event_types_seen": dict(self.event_types_seen),
            "git_sha": self.git_sha,
            "scenario": self.scenario,
            "linked_run_ids": list(self.linked_run_ids),
            "compressed": self.compressed,
        }

    @property
    def event_count(self) -> int:
        return sum(s.event_count for s in self.segments)

    @property
    def gap_count(self) -> int:
        return len(self.gaps)


class EventSink:
    """Async buffered writer for canonical :class:`~tyrex_pm.core.events.MarketEvent` streams."""

    def __init__(
        self,
        output_dir: Path,
        market_id: str,
        *,
        yes_token_id: str | None = None,
        no_token_id: str | None = None,
        queue_maxsize: int = 10_000,
        segment_max_mb: int = 64,
        segment_max_s: int = 300,
        batch_size: int = 100,
        batch_flush_ms: int = 50,
        git_sha: str = "unknown",
        scenario: str = "",
        compress: bool = False,
        on_event_written: Callable[[MarketEvent], None] | None = None,
    ) -> None:
        self._output_dir = Path(output_dir)
        self._market_id = market_id
        self._queue_maxsize = max(1, queue_maxsize)
        self._segment_max_bytes = max(1, segment_max_mb) * 1024 * 1024
        self._segment_max_s = max(1.0, float(segment_max_s))
        self._batch_size = max(1, batch_size)
        self._batch_flush_s = max(0.001, batch_flush_ms / 1000.0)
        self._compress = bool(compress)
        self._on_event_written = on_event_written

        if self._compress:
            _require_zstandard()

        started = _utc_now()
        self._manifest = ManifestState(
            market_id=market_id,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            recording_started_ts=_iso8601(started),
            git_sha=git_sha,
            scenario=scenario,
            compressed=self._compress,
        )

        self._queue: asyncio.Queue[MarketEvent | None] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._segment_index = 0
        self._segment_opened_mono: float | None = None
        self._segment_fp: BinaryIO | None = None
        self._zstd_writer: Any = None
        self._current_segment: SegmentStats | None = None
        self._stopped = False
        self._last_event_recv_ts: str | None = None
        self._last_write_mono: float | None = None

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def manifest(self) -> ManifestState:
        return self._manifest

    @property
    def dropped_events(self) -> int:
        return self._manifest.dropped_events

    @property
    def last_event_recv_ts(self) -> str | None:
        return self._last_event_recv_ts

    @property
    def last_write_mono(self) -> float | None:
        return self._last_write_mono

    async def start(self) -> None:
        if self._writer_task is not None:
            return
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._queue = asyncio.Queue(maxsize=self._queue_maxsize)
        loop = asyncio.get_running_loop()
        self._writer_task = loop.create_task(self._writer_loop())
        self._write_manifest()

    def emit(self, event: MarketEvent) -> None:
        """Non-blocking enqueue. Never awaits. Never touches disk."""
        if self._queue is None or self._stopped:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._manifest.dropped_events += 1

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._queue is not None:
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        if self._writer_task is not None:
            await self._writer_task
            self._writer_task = None
        self._close_segment()
        self._manifest.recording_ended_ts = _iso8601(_utc_now())
        self._write_manifest()

    async def _writer_loop(self) -> None:
        assert self._queue is not None
        batch: list[MarketEvent] = []
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=self._batch_flush_s)
            except asyncio.TimeoutError:
                item = None
            if item is None:
                if batch:
                    self._write_batch(batch)
                    batch = []
                if self._stopped and self._queue.empty():
                    break
                continue
            batch.append(item)
            if len(batch) >= self._batch_size:
                self._write_batch(batch)
                batch = []
        if batch:
            self._write_batch(batch)

    def _write_batch(self, events: list[MarketEvent]) -> None:
        now_mono = time.monotonic()
        for event in events:
            self._ensure_segment_open(now_mono)
            line = json.dumps(event_to_dict(event), separators=(",", ":"), ensure_ascii=False)
            encoded = (line + "\n").encode("utf-8")
            self._write_segment_bytes(encoded)
            recv_iso = _iso8601(event.recv_ts)
            assert self._current_segment is not None
            self._current_segment.event_count += 1
            self._current_segment.bytes += len(encoded)
            if self._current_segment.first_recv_ts is None:
                self._current_segment.first_recv_ts = recv_iso
            self._current_segment.last_recv_ts = recv_iso
            self._last_event_recv_ts = recv_iso
            self._last_write_mono = time.monotonic()
            et = event.event_type.value if isinstance(event.event_type, EventType) else str(event.event_type)
            self._manifest.event_types_seen[et] = self._manifest.event_types_seen.get(et, 0) + 1
            if event.event_type == EventType.WS_SEQ_GAP:
                self._manifest.gaps.append(
                    {
                        "event_id": event.event_id,
                        "token_id": str(event.token_id) if event.token_id is not None else None,
                        "recv_ts": recv_iso,
                        "payload": dict(event.payload),
                    }
                )
            if self._on_event_written is not None:
                self._on_event_written(event)
            self._maybe_rotate_segment(time.monotonic())
        self._write_manifest()

    def _segment_filename(self, index: int) -> str:
        base = f"events-{index:05d}.jsonl"
        return f"{base}.zst" if self._compress else base

    def _ensure_segment_open(self, now_mono: float) -> None:
        if self._segment_fp is not None:
            return
        self._segment_index += 1
        name = self._segment_filename(self._segment_index)
        path = self._output_dir / name
        self._segment_fp = open(path, "wb")  # noqa: SIM115
        if self._compress:
            zstd = _require_zstandard()
            self._zstd_writer = zstd.ZstdCompressor().stream_writer(self._segment_fp)
        self._segment_opened_mono = now_mono
        self._current_segment = SegmentStats(path=name)
        self._manifest.segments.append(self._current_segment)

    def _write_segment_bytes(self, encoded: bytes) -> None:
        if self._compress:
            assert self._zstd_writer is not None
            self._zstd_writer.write(encoded)
        else:
            assert self._segment_fp is not None
            self._segment_fp.write(encoded)

    def _maybe_rotate_segment(self, now_mono: float) -> None:
        if self._segment_fp is None or self._current_segment is None:
            return
        age = now_mono - (self._segment_opened_mono or now_mono)
        if self._current_segment.bytes >= self._segment_max_bytes or age >= self._segment_max_s:
            self._close_segment()

    def _close_segment(self) -> None:
        if self._zstd_writer is not None:
            self._zstd_writer.close()
            self._zstd_writer = None
        if self._segment_fp is not None:
            self._segment_fp.close()
            self._segment_fp = None
        self._segment_opened_mono = None
        self._current_segment = None

    def _write_manifest(self) -> None:
        manifest_path = self._output_dir / "manifest.json"
        payload = json.dumps(self._manifest.to_dict(), indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=self._output_dir, prefix=".manifest-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, manifest_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def read_event_segment_lines(path: Path) -> list[str]:
    """Read plain or zstd-compressed JSONL segment lines for tests/replay prep."""
    if path.suffix == ".zst" or str(path).endswith(".jsonl.zst"):
        zstd = _require_zstandard()
        with open(path, "rb") as fh:
            data = zstd.ZstdDecompressor().stream_reader(fh).read()
        text = data.decode("utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]
