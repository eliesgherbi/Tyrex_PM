"""Disposable, incremental coverage projection of checksum-verified immutable parts.

The archive remains authoritative. Transactions publish one complete part at a time;
changed/compacted input sets or a changed projection rebuild it rather than retaining
stale rows. Rows are stored as compressed per-entity blocks so the projection stays
proportional to the normalized archive instead of carrying per-row index keys.
"""

from __future__ import annotations

import heapq
import sqlite3
from decimal import Decimal
from typing import Iterable, Iterator

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ..capture.files import within
from ..contracts import canonical, decode, file_digest, identity
from ..query.reader import Reader, entity_rows, exact_rows, order

DATASETS = (
    "markets",
    "metadata_versions",
    "instruments",
    "resolution_observations",
    "reference_observations",
    "operational_events",
    "trade_prints",
    "processing_results",
    "book_snapshots",
    "book_updates",
)

# Coverage is a projection, not a second copy of every typed payload. In particular,
# avoid converting unused Arrow envelope fields for millions of book/trade rows.
COMMON = (
    "record_id",
    "payload_path",
    "collection_id",
    "session_id",
    "ingest_sequence",
    "source_id",
    "connection_id",
    "received_at",
)
PROJECT = {
    "markets": ("market_id", "underlying_asset"),
    "metadata_versions": ("entity_id", "window_start", "window_end", "rule_fingerprint", "closed"),
    "instruments": ("market_id", "instrument_id", "outcome_index"),
    "book_snapshots": ("instrument_id", "snapshot_origin", "provider_update_id", "bids", "asks"),
    "book_updates": ("first_update_id", "last_update_id", "changes"),
    "trade_prints": (),
    "operational_events": ("details", "event_type"),
    "processing_results": ("status",),
    "reference_observations": ("feed_id", "source_time", "delivery_kind", "value", "unit"),
}
LAYOUT = "2"
BLOCK_ROWS = 2048
# A changed layout or projection must rebuild; old rows may lack newly needed fields.
PROJECTION_ID = identity(LAYOUT, DATASETS, COMMON, sorted(PROJECT.items()))
FILENAME = "coverage-index-v2.sqlite"


def _compressor():
    import zstandard

    return zstandard.ZstdCompressor(level=3)


def _decompressor():
    import zstandard

    return zstandard.ZstdDecompressor()


def _restore(dataset: str, row: dict) -> dict:
    if dataset == "reference_observations" and row.get("value") is not None:
        row["value"] = Decimal(row["value"])
    if dataset in ("book_snapshots", "book_updates"):
        for key in ("bids", "asks", "changes"):
            for level in row.get(key, []):
                for field in ("price", "quantity", "new_quantity"):
                    if field in level:
                        level[field] = Decimal(level[field])
    return row


class CoverageIndex(Reader):
    def __init__(self, reader: Reader, *, connection: sqlite3.Connection | None = None):
        super().__init__(reader.root, reader.collection, reader.manifests)
        self.read_only = connection is not None
        if connection is not None:
            self.db = connection
            return
        path = self.path(reader)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS parts(path TEXT PRIMARY KEY, sha TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS blocks(
                dataset TEXT NOT NULL, entity TEXT NOT NULL,
                first_at INTEGER NOT NULL, last_at INTEGER NOT NULL,
                rows INTEGER NOT NULL, body BLOB NOT NULL);
            CREATE INDEX IF NOT EXISTS blocks_by_entity ON blocks(dataset, entity, first_at);
        """)

    @staticmethod
    def path(reader: Reader):
        return within(reader.root, f"collections/{reader.collection}/{FILENAME}")

    @staticmethod
    def _expected(reader: Reader) -> dict[str, str]:
        return {
            f["path"]: f["sha256"]
            for b in reader.manifests
            for f in b["files"]
            if f["dataset"] in DATASETS
        }

    @classmethod
    def open_current(cls, reader: Reader) -> CoverageIndex | None:
        """Open without writing or locking, only if it exactly matches this reader.

        One read transaction pins a consistent snapshot while a writer may append.
        """
        path = cls.path(reader)
        if not path.exists():
            return None
        try:
            db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, isolation_level=None)
            db.execute("BEGIN")
            meta = dict(db.execute("SELECT key, value FROM meta"))
            parts = dict(db.execute("SELECT path, sha FROM parts"))
        except sqlite3.Error:
            return None
        if meta.get("projection") != PROJECTION_ID or parts != cls._expected(reader):
            db.close()
            return None
        return cls(reader, connection=db)

    def close(self):
        self.db.close()

    def update(self, progress=None):
        if self.read_only:
            raise ValueError("read-only coverage index")
        files = {
            f["path"]: f for b in self.manifests for f in b["files"] if f["dataset"] in DATASETS
        }
        meta = dict(self.db.execute("SELECT key, value FROM meta"))
        existing = dict(self.db.execute("SELECT path, sha FROM parts"))
        if meta.get("projection") != PROJECTION_ID or any(
            p not in files or files[p]["sha256"] != sha for p, sha in existing.items()
        ):
            with self.db:
                self.db.execute("DELETE FROM blocks")
                self.db.execute("DELETE FROM parts")
                self.db.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('projection', ?)", (PROJECTION_ID,)
                )
            existing = {}
        pending = [f for p, f in files.items() if p not in existing]
        compressor = _compressor()
        for number, file in enumerate(pending):
            path = within(self.root, file["path"])
            if file_digest(path) != file["sha256"]:
                raise ValueError("normalized part checksum mismatch")
            dataset = file["dataset"]
            columns = list(COMMON + PROJECT[dataset]) if dataset in PROJECT else None
            grouped: dict[str, list[dict]] = {}
            for batch in pq.ParquetFile(path).iter_batches(batch_size=4096, columns=columns):
                table = pa.Table.from_batches([batch])
                if dataset == "processing_results":
                    table = table.filter(
                        pc.is_in(table["status"], value_set=pa.array(["REJECTED", "UNKNOWN_TYPE"]))
                    )
                for row in exact_rows(table):
                    for entity, item in entity_rows(dataset, row):
                        grouped.setdefault(entity, []).append(item)
            inserts = []
            for entity, rows in grouped.items():
                rows.sort(key=order)
                for offset in range(0, len(rows), BLOCK_ROWS):
                    chunk = rows[offset : offset + BLOCK_ROWS]
                    inserts.append(
                        (
                            dataset,
                            entity,
                            chunk[0]["received_at"],
                            chunk[-1]["received_at"],
                            len(chunk),
                            compressor.compress(canonical(chunk).encode()),
                        )
                    )
            with self.db:
                self.db.executemany("INSERT INTO blocks VALUES (?,?,?,?,?,?)", inserts)
                self.db.execute("INSERT INTO parts VALUES (?,?)", (file["path"], file["sha256"]))
            if progress:
                progress(number + 1, len(pending))

    def iter_rows(
        self,
        dataset: str,
        start: int | None = None,
        until: int | None = None,
        entities: Iterable[str] | None = None,
    ) -> Iterator[dict]:
        if entities is None and dataset == "book_updates":
            raise ValueError("indexed book updates are split by instrument; pass entities")
        where, args = ["dataset = ?"], [dataset]
        if entities is not None:
            keys = sorted(set(entities))
            if not keys:
                return
            where.append("entity IN (" + ",".join("?" for _ in keys) + ")")
            args.extend(keys)
        if start is not None:
            where.append("last_at >= ?")
            args.append(start)
        if until is not None:
            where.append("first_at <= ?")
            args.append(until)
        cursor = self.db.execute(
            "SELECT first_at, body FROM blocks WHERE "
            + " AND ".join(where)
            + " ORDER BY first_at, rowid",
            args,
        )
        decompressor = _decompressor()

        def rows(body: bytes) -> Iterator[dict]:
            for row in decode(decompressor.decompress(body)):
                at = row["received_at"]
                if (start is None or at >= start) and (until is None or at <= until):
                    yield _restore(dataset, row)

        # Each block is sorted; open a block only when the merge frontier reaches it.
        heap, serial = [], 0
        block = cursor.fetchone()
        while block is not None or heap:
            while block is not None and (not heap or block[0] <= heap[0][0][0]):
                stream = rows(block[1])
                block = cursor.fetchone()
                row = next(stream, None)
                if row is not None:
                    serial += 1
                    heapq.heappush(heap, (order(row), serial, row, stream))
            if heap:
                _, number, row, stream = heapq.heappop(heap)
                yield row
                row = next(stream, None)
                if row is not None:
                    heapq.heappush(heap, (order(row), number, row, stream))
