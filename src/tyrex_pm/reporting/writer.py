"""Batched JSONL writers, atomic JSON replace, durable critical acknowledgements."""

from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from tyrex_pm.reporting.contracts import ReportingEvent, ReportingLane


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Crash-safe replace: temp write → flush/fsync → atomic replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    with tmp.open("w", encoding="utf-8", newline="\n") as fp:
        fp.write(text)
        if not text.endswith("\n"):
            fp.write("\n")
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, path)


@dataclass
class LaneWriteResult:
    accepted: bool
    persisted: bool
    dropped: bool = False
    error: str | None = None
    sequence: int | None = None


@dataclass
class ReportingWriter:
    """Two independent lane writers; analytics pressure never blocks critical."""

    audit_path: Path
    analytics_path: Path
    debug_path: Path | None = None
    analytics_max_queue: int = 1024
    critical_max_queue: int = 4096
    batch_size: int = 32
    on_critical_failure: Callable[[str], None] | None = None

    _audit_fp: Any = field(default=None, init=False, repr=False)
    _analytics_fp: Any = field(default=None, init=False, repr=False)
    _debug_fp: Any = field(default=None, init=False, repr=False)
    _analytics_buf: deque[ReportingEvent] = field(default_factory=deque, init=False)
    _critical_buf: deque[ReportingEvent] = field(default_factory=deque, init=False)
    closed: bool = field(default=False, init=False)
    last_durable_sequence: int = field(default=0, init=False)
    analytics_dropped: int = field(default=0, init=False)
    critical_failures: int = field(default=0, init=False)
    audit_written: int = field(default=0, init=False)
    analytics_written: int = field(default=0, init=False)
    fail_critical_writes: bool = field(default=False, init=False)

    def open(self) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self.analytics_path.parent.mkdir(parents=True, exist_ok=True)
        self._audit_fp = self.audit_path.open("a", encoding="utf-8", newline="\n")
        self._analytics_fp = self.analytics_path.open("a", encoding="utf-8", newline="\n")
        if self.debug_path is not None:
            self.debug_path.parent.mkdir(parents=True, exist_ok=True)
            self._debug_fp = self.debug_path.open("a", encoding="utf-8", newline="\n")

    def write_event(self, event: ReportingEvent, *, durable: bool = False) -> LaneWriteResult:
        if self.closed:
            return LaneWriteResult(
                accepted=False, persisted=False, error="writer_closed", sequence=event.sequence
            )
        if event.lane is ReportingLane.CRITICAL:
            return self._write_critical(event, durable=durable)
        return self._write_analytics(event)

    def _write_critical(self, event: ReportingEvent, *, durable: bool) -> LaneWriteResult:
        if self.fail_critical_writes:
            self.critical_failures += 1
            msg = "forced_critical_write_failure"
            if self.on_critical_failure:
                self.on_critical_failure(msg)
            return LaneWriteResult(
                accepted=False, persisted=False, error=msg, sequence=event.sequence
            )
        try:
            if len(self._critical_buf) >= self.critical_max_queue and not durable:
                # Never drop critical — force flush instead.
                self._flush_critical()
            self._critical_buf.append(event)
            if durable or len(self._critical_buf) >= self.batch_size:
                self._flush_critical()
            return LaneWriteResult(
                accepted=True, persisted=durable or True, sequence=event.sequence
            )
        except Exception as exc:  # noqa: BLE001 — surface as critical failure
            self.critical_failures += 1
            msg = f"critical_write_failed:{exc}"
            if self.on_critical_failure:
                self.on_critical_failure(msg)
            return LaneWriteResult(
                accepted=False, persisted=False, error=msg, sequence=event.sequence
            )

    def _write_analytics(self, event: ReportingEvent) -> LaneWriteResult:
        if len(self._analytics_buf) >= self.analytics_max_queue:
            self.analytics_dropped += 1
            return LaneWriteResult(
                accepted=False,
                persisted=False,
                dropped=True,
                error="analytics_backpressure",
                sequence=event.sequence,
            )
        self._analytics_buf.append(event)
        if len(self._analytics_buf) >= self.batch_size:
            self._flush_analytics()
        return LaneWriteResult(accepted=True, persisted=False, sequence=event.sequence)

    def persist_critical_ack(self, event: ReportingEvent) -> LaneWriteResult:
        """Durably append a critical event and fsync before returning ack."""
        if self.closed:
            return LaneWriteResult(
                accepted=False, persisted=False, error="writer_closed", sequence=event.sequence
            )
        if self.fail_critical_writes:
            self.critical_failures += 1
            msg = "forced_critical_write_failure"
            if self.on_critical_failure:
                self.on_critical_failure(msg)
            return LaneWriteResult(
                accepted=False, persisted=False, error=msg, sequence=event.sequence
            )
        try:
            self._critical_buf.append(event)
            self._flush_critical(fsync=True)
            return LaneWriteResult(
                accepted=True, persisted=True, sequence=event.sequence
            )
        except Exception as exc:  # noqa: BLE001
            self.critical_failures += 1
            msg = f"critical_ack_failed:{exc}"
            if self.on_critical_failure:
                self.on_critical_failure(msg)
            return LaneWriteResult(
                accepted=False, persisted=False, error=msg, sequence=event.sequence
            )

    def flush(self) -> None:
        self._flush_critical(fsync=True)
        self._flush_analytics()

    def _flush_critical(self, *, fsync: bool = False) -> None:
        if self._audit_fp is None:
            return
        while self._critical_buf:
            event = self._critical_buf.popleft()
            line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
            self._audit_fp.write(line + "\n")
            self.audit_written += 1
            self.last_durable_sequence = max(self.last_durable_sequence, event.sequence)
        self._audit_fp.flush()
        if fsync:
            os.fsync(self._audit_fp.fileno())

    def _flush_analytics(self) -> None:
        if self._analytics_fp is None:
            return
        while self._analytics_buf:
            event = self._analytics_buf.popleft()
            line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
            target = self._analytics_fp
            if (
                self._debug_fp is not None
                and str(event.payload.get("debug") or event.event_family) == "debug"
            ):
                target = self._debug_fp
            # Host-trace debug events go to debug file when present.
            if self._debug_fp is not None and event.event_type.startswith("debug."):
                target = self._debug_fp
            target.write(line + "\n")
            self.analytics_written += 1
        self._analytics_fp.flush()
        if self._debug_fp is not None:
            self._debug_fp.flush()

    def close(self) -> None:
        if self.closed:
            return
        try:
            self.flush()
        finally:
            self.closed = True
            for fp in (self._audit_fp, self._analytics_fp, self._debug_fp):
                if fp is not None:
                    fp.close()
            self._audit_fp = None
            self._analytics_fp = None
            self._debug_fp = None
