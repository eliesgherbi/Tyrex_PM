from __future__ import annotations

import heapq
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator

import pyarrow as pa
import pyarrow.parquet as pq

from ..capture.files import within
from ..contracts import file_digest
from ..normalize.worker import committed
from ..reconstruction.books import Books


def exact_rows(table: pa.Table) -> list[dict]:
    for n, field in enumerate(table.schema):
        if pa.types.is_timestamp(field.type):
            table = table.set_column(n, field.name, table.column(n).cast(pa.int64()))
    return table.to_pylist()


BINANCE = "binance:spot:BTCUSDT"


def entity_rows(dataset: str, row: dict) -> list[tuple[str, dict]]:
    """Entity key(s) used to bound physical reads. Shared by the index and Reader."""
    if dataset == "book_updates":
        grouped: dict[str, list] = {}
        for change in row["changes"]:
            grouped.setdefault(change["instrument_id"], []).append(change)
        if row["source_id"] == "binance":
            grouped.setdefault(BINANCE, [])
        return [(iid, {**row, "changes": changes}) for iid, changes in grouped.items()]
    if dataset == "book_snapshots":
        return [(row["instrument_id"], row)]
    if dataset == "metadata_versions":
        return [(row.get("entity_id") or "", row)]
    if dataset in ("markets", "instruments", "resolution_observations"):
        return [(row.get("market_id") or "", row)]
    return [("", row)]


def order(row: dict) -> tuple:
    return (
        row["received_at"],
        row["session_id"],
        row["ingest_sequence"],
        row["payload_path"],
        row["record_id"],
    )


class Reader:
    def __init__(
        self,
        root: Path,
        collection: str | None = None,
        manifests: list[dict] | None = None,
        *,
        merge_policy: str = "wall_session_sequence",
    ):
        if merge_policy != "wall_session_sequence":
            raise ValueError("explicit wall_session_sequence ordering required")
        self.root = root.resolve()
        self.collection = collection
        self.manifests = committed(self.root, collection) if manifests is None else manifests
        versions = {(b["schema_version"], b["parser_version"]) for b in self.manifests}
        if len(versions) > 1 or any(v[0] != "1" for v in versions):
            raise ValueError("mixed or unsupported dataset versions")
        self.merge_policy = merge_policy

    def iter_rows(
        self,
        dataset: str,
        start: int | None = None,
        until: int | None = None,
        entities: Iterable[str] | None = None,
    ) -> Iterator[dict]:
        if entities is None:
            yield from self._iter_rows(dataset, start, until)
            return
        keys = set(entities)
        for row in self._iter_rows(dataset, start, until):
            for entity, item in entity_rows(dataset, row):
                if entity in keys:
                    yield item

    def _iter_rows(
        self, dataset: str, start: int | None = None, until: int | None = None
    ) -> Iterator[dict]:
        parts = []
        for build in self.manifests:
            if start is not None and build["last_received_at"] < start:
                continue
            if until is not None and build["first_received_at"] > until:
                continue
            for file in build["files"]:
                if file["dataset"] == dataset:
                    left = file.get("first_received_at", build["first_received_at"])
                    right = file.get("last_received_at", build["last_received_at"])
                    if (until is None or left <= until) and (start is None or right >= start):
                        parts.append((left, file["path"], file))
        parts.sort(key=lambda p: (p[0], p[1]))
        heap, next_part, serial = [], 0, 0
        while next_part < len(parts) or heap:
            # Open only parts whose time bounds can contain the next row.
            while next_part < len(parts) and (not heap or parts[next_part][0] <= heap[0][0][0]):
                stream = self._part(parts[next_part][2], start, until)
                next_part += 1
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

    def _part(self, file: dict, start: int | None, until: int | None) -> Iterator[dict]:
        path = within(self.root, file["path"])
        if file_digest(path) != file["sha256"]:
            raise ValueError("normalized part checksum mismatch")
        # Each immutable part is sorted at publication; batching bounds reader memory.
        for batch in pq.ParquetFile(path).iter_batches(batch_size=64):
            for row in exact_rows(pa.Table.from_batches([batch])):
                if until is not None and row["received_at"] > until:
                    return
                if (start is None or row["received_at"] >= start) and (
                    until is None or row["received_at"] <= until
                ):
                    yield row

    def list_markets(self, selector: str | None = None, observed_before: int | None = None) -> list:
        latest = {}
        for row in self.iter_rows("markets", until=observed_before):
            if selector is None or selector in (row["market_id"], row["underlying_asset"]):
                latest[row["market_id"]] = row
        return list(latest.values())

    def get_market_history(self, market_id: str, until: int | None = None) -> list:
        return [
            r
            for r in self.iter_rows("metadata_versions", until=until)
            if r["entity_id"] == market_id
        ]

    def events(
        self, datasets: tuple[str, ...], until: int | None = None, start: int | None = None
    ) -> Iterator[tuple[str, dict]]:
        def tagged(name: str) -> Iterator:
            for row in self.iter_rows(name, start, until):
                yield name, row

        yield from heapq.merge(*(tagged(d) for d in datasets), key=lambda item: order(item[1]))

    def book_as_observed(self, instrument: str, received_at: int) -> dict:
        state = Books()
        for dataset, row in self.events(
            ("book_snapshots", "book_updates", "operational_events"), received_at
        ):
            state.apply(dataset, row)
        book = state.books.get(instrument)
        return (
            book.view()
            if book
            else {
                "instrument_id": instrument,
                "valid": False,
                "reason": "no_seed",
                "bids": [],
                "asks": [],
            }
        )

    def references_as_observed(self, market: str, received_at: int) -> list:
        feeds = {
            r["feed_id"]
            for r in self.iter_rows("feed_bindings", until=received_at)
            if r["market_id"] == market
        }
        return [
            r
            for r in self.iter_rows("reference_observations", until=received_at)
            if r["feed_id"] in feeds
        ]

    def coverage(self, market: str, *, as_of: int | None = None) -> dict:
        from ..quality.coverage import coverage

        return coverage(self, market, as_of=as_of)

    def export_market(self, market: str, output: Path) -> dict:
        from .export import export_market

        return export_market(self, market, output)

    def query_sql(self, dataset: str, query: str, *, received_until: int | None = None) -> list:
        """Read-only SQL on a named exact-text projection; external access is disabled."""
        import duckdb

        from ..normalize.schemas import schema

        table = pa.Table.from_pylist(
            list(self.iter_rows(dataset, until=received_until)), schema=schema(dataset)
        )
        with duckdb.connect(config={"enable_external_access": False}) as connection:
            statements = connection.extract_statements(query)
            if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                raise ValueError("SQL facade accepts one SELECT statement")
            connection.register("observations", sql_projection(table))
            return connection.execute(query).fetchall()

    def read_observations(
        self, market: str, received_from: int, received_until: int, datasets: tuple[str, ...]
    ) -> Iterator:
        instruments = {
            r["instrument_id"] for r in self.iter_rows("instruments") if r["market_id"] == market
        }
        feeds = {r["feed_id"] for r in self.iter_rows("feed_bindings") if r["market_id"] == market}
        for dataset, row in self.events(datasets, received_until, received_from):
            if (
                row.get("market_id") == market
                or row.get("entity_id") == market
                or row.get("entity_id") in instruments  # token-level market changes
                or row.get("instrument_id") in instruments
                or row.get("feed_id") in feeds
                or row["source_id"] == "binance"
            ):
                yield dataset, row


def sql_projection(table: pa.Table) -> pa.Table:
    """Explicit text decimals and epoch-nanosecond times; never a lossy SQL cast."""

    def project_type(dtype: pa.DataType) -> pa.DataType:
        if pa.types.is_decimal(dtype):
            return pa.string()
        if pa.types.is_timestamp(dtype):
            return pa.int64()
        if pa.types.is_list(dtype):
            return pa.list_(project_type(dtype.value_type))
        if pa.types.is_struct(dtype):
            return pa.struct([(f.name, project_type(f.type)) for f in dtype])
        return dtype

    def project(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {k: project(v) for k, v in value.items()}
        if isinstance(value, list):
            return [project(v) for v in value]
        return value

    rows = [project(row) for row in exact_rows(table)]
    return (
        pa.Table.from_pylist(
            rows,
            schema=pa.schema(
                [
                    (
                        f.name + "_epoch_ns" if pa.types.is_timestamp(f.type) else f.name,
                        project_type(f.type),
                    )
                    for f in table.schema
                ]
            ),
        )
        if not rows
        else pa.Table.from_pylist(
            [
                {
                    (k + "_epoch_ns" if pa.types.is_timestamp(table.schema.field(k).type) else k): v
                    for k, v in row.items()
                }
                for row in rows
            ],
            schema=pa.schema(
                [
                    (
                        f.name + "_epoch_ns" if pa.types.is_timestamp(f.type) else f.name,
                        project_type(f.type),
                    )
                    for f in table.schema
                ]
            ),
        )
    )
