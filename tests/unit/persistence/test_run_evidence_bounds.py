"""Run evidence: bounded per-evaluation events, capture-time timestamps, schema meta."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tyrex_pm.persistence.run_evidence_journal import (
    RunEvidenceRecorder,
    SqliteRunEvidenceJournal,
)


@pytest.mark.asyncio
async def test_per_evaluation_events_are_bounded_and_counted(tmp_path: Path) -> None:
    journal = SqliteRunEvidenceJournal(tmp_path / "j.sqlite3", run_instance_id="r")
    recorder = RunEvidenceRecorder(journal, max_droppable_queued=3)
    await recorder.start()
    for i in range(10):  # no await: the writer cannot drain in between
        recorder.record("STRATEGY_DECISION", {"i": i})
    recorder.record("ENTRY_OUTCOME", {"kind": "FILLED"})  # never dropped
    assert recorder.status()["dropped"] == {"STRATEGY_DECISION": 7}
    records = await recorder.snapshot_and_close()
    assert [r.event_type for r in records].count("STRATEGY_DECISION") == 3
    assert any(r.event_type == "ENTRY_OUTCOME" for r in records)
    assert any("run_evidence_dropped" in f for f in recorder.failures)


@pytest.mark.asyncio
async def test_timestamp_is_the_capture_time_and_schema_is_recorded(tmp_path: Path) -> None:
    path = tmp_path / "j.sqlite3"
    journal = SqliteRunEvidenceJournal(path, run_instance_id="r")
    recorder = RunEvidenceRecorder(journal)
    await recorder.start()
    captured = datetime.now(timezone.utc)
    recorder.record("A", {})
    await asyncio.sleep(0.2)
    (record,) = await recorder.snapshot_and_close()
    assert abs((record.observed_at - captured).total_seconds()) < 0.1
    connection = sqlite3.connect(path)
    meta = dict(connection.execute("SELECT key, value FROM run_events_meta"))
    connection.close()
    assert meta == {"schema_version": "2", "observed_at_basis": "capture"}
