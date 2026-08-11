"""SQLite-backed authoritative execution event journal."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Iterable, Protocol

from tyrex_pm.execution.evidence import (
    ExecutionEvidence,
    SubmissionAttempted,
    evidence_from_dict,
    evidence_to_dict,
)


class JournalError(RuntimeError):
    pass


class ExecutionJournal(Protocol):
    def append(self, event: ExecutionEvidence) -> bool: ...
    def load(self, session_id: str) -> tuple[ExecutionEvidence, ...]: ...
    def mutation_attempt_count(self, session_id: str | None = None) -> int: ...
    def sessions(self) -> tuple[str, ...]: ...
    def close(self) -> None: ...


@dataclass
class MemoryExecutionJournal:
    """Deterministic test adapter; never selected by production composition."""

    _events: list[ExecutionEvidence]
    _dedupe: set[tuple[str, str]]

    def __init__(self) -> None:
        self._events = []
        self._dedupe = set()

    def append(self, event: ExecutionEvidence) -> bool:
        key = (event.session_id, event.dedupe_key)
        if key in self._dedupe:
            return False
        self._dedupe.add(key)
        self._events.append(event)
        return True

    def load(self, session_id: str) -> tuple[ExecutionEvidence, ...]:
        return tuple(event for event in self._events if event.session_id == session_id)

    def mutation_attempt_count(self, session_id: str | None = None) -> int:
        return sum(
            isinstance(event, SubmissionAttempted)
            for event in self._events
            if session_id is None or event.session_id == session_id
        )

    def sessions(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(event.session_id for event in self._events))

    def close(self) -> None:
        return None


class SqliteExecutionJournal:
    """Append-only journal using one SQLite WAL database.

    Event identity and semantic deduplication are enforced transactionally.  A
    dispatch-attempt event is therefore durable before the coordinator begins
    the network POST.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS journal_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(session_id, dedupe_key)
                );
                CREATE INDEX IF NOT EXISTS ix_execution_events_session_sequence
                    ON execution_events(session_id, sequence);
                CREATE TABLE IF NOT EXISTS execution_checkpoints (
                    session_id TEXT PRIMARY KEY,
                    through_sequence INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            row = self._db.execute(
                "SELECT value FROM journal_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO journal_meta(key, value) VALUES('schema_version', ?)",
                    (str(self.SCHEMA_VERSION),),
                )
            elif int(row[0]) != self.SCHEMA_VERSION:
                raise JournalError(
                    f"unsupported execution journal schema {row[0]} "
                    f"(expected {self.SCHEMA_VERSION})"
                )

    def append(self, event: ExecutionEvidence) -> bool:
        payload = evidence_to_dict(event)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    """
                    INSERT INTO execution_events(
                        event_id, session_id, event_type, dedupe_key,
                        observed_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.session_id,
                        type(event).__name__,
                        event.dedupe_key,
                        event.observed_at.isoformat(),
                        encoded,
                    ),
                )
            except sqlite3.IntegrityError:
                self._db.execute("ROLLBACK")
                return False
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")
                return True

    def load(self, session_id: str) -> tuple[ExecutionEvidence, ...]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT payload_json
                FROM execution_events
                WHERE session_id=?
                ORDER BY sequence ASC
                """,
                (session_id,),
            ).fetchall()
        return tuple(evidence_from_dict(json.loads(row[0])) for row in rows)

    def sessions(self) -> tuple[str, ...]:
        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT session_id FROM execution_events ORDER BY session_id"
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def mutation_attempt_count(self, session_id: str | None = None) -> int:
        query = "SELECT COUNT(*) FROM execution_events WHERE event_type=?"
        params: list[str] = [SubmissionAttempted.__name__]
        if session_id is not None:
            query += " AND session_id=?"
            params.append(session_id)
        with self._lock:
            row = self._db.execute(query, tuple(params)).fetchone()
        return int(row[0]) if row is not None else 0

    def iter_all(self) -> Iterable[ExecutionEvidence]:
        with self._lock:
            rows = self._db.execute(
                "SELECT payload_json FROM execution_events ORDER BY sequence ASC"
            ).fetchall()
        for row in rows:
            yield evidence_from_dict(json.loads(row[0]))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "SqliteExecutionJournal":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
