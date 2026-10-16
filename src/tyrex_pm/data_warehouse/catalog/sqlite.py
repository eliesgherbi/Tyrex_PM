from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..capture.writer import raw_manifests, segment_records
from ..contracts import RawEnvelope, canonical, decode


class Catalog:
    """Projection only: every state mutation follows a durable collector event."""

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.db = sqlite3.connect(root / "catalog.sqlite", timeout=2)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS projection (
              collection TEXT, namespace TEXT, key TEXT, value TEXT,
              record_id TEXT, received_at INTEGER,
              PRIMARY KEY(collection, namespace, key));
            CREATE TABLE IF NOT EXISTS applied (record_id TEXT PRIMARY KEY);
        """)

    def apply(self, record: RawEnvelope) -> None:
        if record.transport != "collector_event":
            return
        event = decode(record.bytes())
        if event.get("type") not in ("projection", "projection_batch"):
            return
        with self.db:
            if self.db.execute(
                "SELECT 1 FROM applied WHERE record_id=?", (record.raw_record_id,)
            ).fetchone():
                return
            updates = event["updates"] if event["type"] == "projection_batch" else [event]
            for update in updates:
                self.db.execute(
                    "INSERT OR REPLACE INTO projection VALUES(?,?,?,?,?,?)",
                    (
                        record.collection_id,
                        update["namespace"],
                        update["key"],
                        canonical(update["value"]),
                        record.raw_record_id,
                        record.received_at,
                    ),
                )
            self.db.execute("INSERT INTO applied VALUES(?)", (record.raw_record_id,))

    def rebuild(self) -> None:
        for manifest in raw_manifests(self.root):
            if manifest["source_id"] == "collector":
                for record in segment_records(self.root, manifest):
                    self.apply(record)
        spool = self.root / "spool"
        if not spool.exists():
            return
        for path in sorted(spool.rglob("*.open.jsonl")):
            for line in path.read_bytes().splitlines():
                try:
                    record = RawEnvelope(**json.loads(line))
                    record.bytes()
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    continue
                if record.source_id == "collector":
                    self.apply(record)

    def values(self, collection: str, namespace: str) -> dict[str, Any]:
        rows = self.db.execute(
            "SELECT key,value FROM projection WHERE collection=? AND namespace=?",
            (collection, namespace),
        )
        return {key: json.loads(value) for key, value in rows}

    def close(self) -> None:
        self.db.close()
