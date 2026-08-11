"""Durable reporting evidence that never participates in trading decisions."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Mapping
from uuid import uuid4


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RunEvidenceRecord:
    sequence: int
    event_id: str
    run_instance_id: str
    event_type: str
    observed_at: datetime
    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event_id": self.event_id,
            "run_instance_id": self.run_instance_id,
            "event_type": self.event_type,
            "observed_at": self.observed_at.isoformat(),
            "payload": dict(self.payload),
        }


class SqliteRunEvidenceJournal:
    """Append-only run evidence stored beside authoritative execution events.

    These events explain a run, but never authorize an order or derive account
    state.  Execution authority remains exclusively in ``execution_events``.
    """

    def __init__(self, path: Path | str, *, run_instance_id: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_instance_id = run_instance_id
        self._lock = RLock()
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS run_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                run_instance_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_run_events_instance_sequence
            ON run_events(run_instance_id, sequence)
            """
        )

    def append(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        observed_at: datetime | None = None,
    ) -> None:
        if not event_type.strip():
            raise ValueError("event_type must be non-empty")
        timestamp = observed_at or _utc_now()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("run evidence timestamps must be timezone-aware")
        encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str)
        with self._lock:
            self._db.execute(
                """
                INSERT INTO run_events(
                    event_id, run_instance_id, event_type, observed_at, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    self.run_instance_id,
                    event_type,
                    timestamp.astimezone(timezone.utc).isoformat(),
                    encoded,
                ),
            )

    def load(self) -> tuple[RunEvidenceRecord, ...]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT sequence, event_id, run_instance_id, event_type,
                       observed_at, payload_json
                FROM run_events
                WHERE run_instance_id=?
                ORDER BY sequence
                """,
                (self.run_instance_id,),
            ).fetchall()
        return tuple(
            RunEvidenceRecord(
                sequence=int(sequence),
                event_id=str(event_id),
                run_instance_id=str(run_instance_id),
                event_type=str(event_type),
                observed_at=datetime.fromisoformat(str(observed_at)),
                payload=json.loads(str(payload_json)),
            )
            for sequence, event_id, run_instance_id, event_type, observed_at, payload_json in rows
        )

    def close(self) -> None:
        with self._lock:
            self._db.close()


class RunEvidenceRecorder:
    """Non-blocking producer with one background durable writer."""

    def __init__(self, journal: SqliteRunEvidenceJournal) -> None:
        self.journal = journal
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
        self._writer_task: asyncio.Task[None] | None = None
        self.failures: list[str] = []

    async def start(self) -> None:
        if self._writer_task is None:
            self._writer_task = asyncio.create_task(self._writer(), name="run-evidence-writer")

    def record(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self._writer_task is None:
            raise RuntimeError("run evidence recorder is not started")
        self._queue.put_nowait((event_type, dict(payload)))

    async def _writer(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            event_type, payload = item
            try:
                await asyncio.to_thread(self.journal.append, event_type, payload)
            except Exception as exc:  # reporting failure must not stop trading
                self.failures.append(f"{event_type}:{type(exc).__name__}:{exc}")
            finally:
                self._queue.task_done()

    async def flush(self) -> None:
        await self._queue.join()

    async def snapshot_and_close(self) -> tuple[RunEvidenceRecord, ...]:
        if self._writer_task is not None:
            await self.flush()
            await self._queue.put(None)
            await self._writer_task
            self._writer_task = None
        records = self.journal.load()
        self.journal.close()
        return records
