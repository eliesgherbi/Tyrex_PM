"""Manifest publication is the transaction; files without manifests are invisible."""

from __future__ import annotations

import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ..capture.files import Ownership, atomic_json, read_json, replace, within
from ..capture.writer import raw_manifests, segment_records
from ..contracts import PARSER_VERSION, SCHEMA_VERSION, canonical, digest, identity
from .registry import normalize
from .schemas import dictionary, schema


def committed(
    root: Path, collection: str | None = None, parser: str = PARSER_VERSION
) -> list[dict[str, Any]]:
    builds = []
    for path in sorted((root / "builds").glob("*/manifest.json")):
        value = read_json(path)
        if value["parser_version"] == parser and (
            collection is None or value["collection_id"] == collection
        ):
            builds.append(value)
    superseded = {old for build in builds for old in build.get("supersedes", [])}
    return [build for build in builds if build["build_id"] not in superseded]


def logical_checksum(table: pa.Table) -> str:
    projected = table
    for index, field in enumerate(projected.schema):
        if pa.types.is_timestamp(field.type):
            projected = projected.set_column(
                index, field.name, projected.column(index).cast(pa.int64())
            )
    return digest(canonical(projected.to_pylist()))


def write_table(path: Path, table: pa.Table) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("wb") as sink:
        pq.write_table(table, sink, compression="zstd", row_group_size=65536)
    with temp.open("r+b") as handle:
        os.fsync(handle.fileno())
    with temp.open("rb") as source:
        check = pq.ParquetFile(source).read()
    if not check.equals(table):
        raise ValueError("Parquet round-trip mismatch")
    replace(temp, path)
    result = {"sha256": digest(path.read_bytes()), "rows": table.num_rows}
    if table.num_rows and "received_at" in table.column_names:
        bounds = pc.min_max(table["received_at"].cast(pa.int64())).as_py()
        result.update(first_received_at=bounds["min"], last_received_at=bounds["max"])
    return result


def normalize_once(root: Path, collection: str | None = None) -> dict:
    result = {"new_builds": 0, "records": 0, "rejected": 0}
    with Ownership(root, "normalizer"):
        done = {
            segment for build in committed(root, collection) for segment in build["input_segments"]
        }
        for manifest in raw_manifests(root, collection):
            if manifest["segment_id"] in done:
                continue
            groups: dict[tuple[str, str], list] = defaultdict(list)
            for raw in segment_records(root, manifest):
                day = datetime.fromtimestamp(
                    raw.received_at // 1_000_000_000, timezone.utc
                ).strftime("%Y-%m-%d")
                for dataset, row in normalize(raw):
                    groups[dataset, day].append(row)
                    result["records"] += 1
                    if dataset == "processing_results" and row["status"] == "REJECTED":
                        result["rejected"] += 1
            # Fast batch validation, with a slow isolation path only for malformed data.
            rejected_raw = set()
            for (dataset, _), rows in groups.items():
                try:
                    pa.Table.from_pylist(rows, schema=schema(dataset))
                except (pa.ArrowException, TypeError, ValueError, OverflowError):
                    for row in rows:
                        try:
                            pa.Table.from_pylist([row], schema=schema(dataset))
                        except (pa.ArrowException, TypeError, ValueError, OverflowError):
                            rejected_raw.add(row["raw_record_id"])
            if rejected_raw:
                for key, rows in list(groups.items()):
                    dataset, _ = key
                    if dataset == "processing_results":
                        for row in rows:
                            if row["raw_record_id"] in rejected_raw:
                                row.update(
                                    status="REJECTED",
                                    emitted_record_ids=[],
                                    emitted_datasets=[],
                                    reasons=["arrow_schema_violation"],
                                )
                    else:
                        groups[key] = [r for r in rows if r["raw_record_id"] not in rejected_raw]
                result["rejected"] += len(rejected_raw)
            build_id = identity(manifest["sha256"], PARSER_VERSION, SCHEMA_VERSION)
            files = []
            logical = {}
            for (dataset, day), rows in sorted(groups.items()):
                if not rows:
                    continue
                rows.sort(
                    key=lambda r: (
                        r["received_at"],
                        r["session_id"],
                        r["ingest_sequence"],
                        r["payload_path"],
                        r["record_id"],
                    )
                )
                table = pa.Table.from_pylist(rows, schema=schema(dataset))
                relative = (
                    f"normalized/schema=v{SCHEMA_VERSION}/dataset={dataset}/"
                    f"date={day}/{build_id}.parquet"
                )
                details = write_table(within(root, relative), table)
                files.append({"path": relative, "dataset": dataset, **details})
                logical[f"{dataset}/{day}"] = digest(canonical(rows))
            build = {
                "manifest_version": 1,
                "build_id": build_id,
                "collection_id": manifest["collection_id"],
                "schema_version": SCHEMA_VERSION,
                "parser_version": PARSER_VERSION,
                "input_segments": [manifest["segment_id"]],
                "input_checksums": {manifest["segment_id"]: manifest["sha256"]},
                "files": files,
                "logical_checksums": logical,
                "first_received_at": manifest["first_received_at"],
                "last_received_at": manifest["last_received_at"],
                "committed_at": time.time_ns(),
                "supersedes": [],
            }
            atomic_json(root / "builds" / build_id / "manifest.json", build)
            result["new_builds"] += 1
        atomic_json(root / "schema_dictionary.json", dictionary())
        builds = committed(root, collection)
        atomic_json(
            root / "normalization_status.json",
            {
                "collection_id": collection,
                "updated_at": time.time_ns(),
                "builds": len(builds),
                "last_received_at": max((b["last_received_at"] for b in builds), default=None),
                "normalized_rows": sum(f["rows"] for b in builds for f in b["files"]),
                "parser_version": PARSER_VERSION,
                "schema_version": SCHEMA_VERSION,
            },
        )
    return result


def compact(root: Path, collection: str, *, max_part_rows: int = 8192) -> dict:
    """Immutable replacement build; old manifests/files remain valid for pinned readers."""
    with Ownership(root, "normalizer"):
        builds = committed(root, collection)
        if len(builds) < 2:
            return {"compacted": 0}
        if max_part_rows < 1:
            raise ValueError("positive compaction row bound required")
        config_path = within(root, f"collections/{collection}/collection.json")
        config = read_json(config_path) if config_path.exists() else {}
        target_bytes = (
            config.get("profile", {}).get("storage", {}).get("normalized_target_mib", 128) * 1024**2
        )
        build_id = identity(
            "compaction-v2", sorted(b["build_id"] for b in builds), target_bytes, max_part_rows
        )
        groups: dict[tuple[str, str], list] = defaultdict(list)
        for build in builds:
            for file in build["files"]:
                # Only paths/metadata are retained; payloads are streamed one part at a time.
                day = next(
                    part.removeprefix("date=")
                    for part in Path(file["path"]).parts
                    if part.startswith("date=")
                )
                groups[file["dataset"], day].append(
                    {
                        **file,
                        "first_received_at": file.get(
                            "first_received_at", build["first_received_at"]
                        ),
                    }
                )
        files, logical = [], {}
        for (dataset, day), inputs in sorted(groups.items()):
            buffers, buffered_rows, buffered_bytes, part_number = [], 0, 0, 0

            def flush() -> None:
                nonlocal buffers, buffered_rows, buffered_bytes, part_number
                if not buffers:
                    return
                table = pa.concat_tables(buffers).sort_by(
                    [
                        ("received_at", "ascending"),
                        ("session_id", "ascending"),
                        ("ingest_sequence", "ascending"),
                        ("payload_path", "ascending"),
                        ("record_id", "ascending"),
                    ]
                )
                relative = (
                    f"normalized/schema=v1/dataset={dataset}/date={day}/"
                    f"{build_id}-{part_number:06d}.parquet"
                )
                files.append(
                    {
                        "path": relative,
                        "dataset": dataset,
                        **write_table(within(root, relative), table),
                    }
                )
                logical[f"{dataset}/{day}/{part_number:06d}"] = logical_checksum(table)
                part_number += 1
                buffers, buffered_rows, buffered_bytes = [], 0, 0

            for file in sorted(inputs, key=lambda f: (f["first_received_at"], f["path"])):
                path = within(root, file["path"])
                from ..contracts import file_digest

                if file_digest(path) != file["sha256"]:
                    raise ValueError("cannot compact corrupted part")
                for batch in pq.ParquetFile(path).iter_batches(batch_size=min(256, max_part_rows)):
                    table = pa.Table.from_batches([batch])
                    if buffered_rows and (
                        buffered_rows + table.num_rows > max_part_rows
                        or buffered_bytes + table.nbytes > target_bytes
                    ):
                        flush()
                    buffers.append(table)
                    buffered_rows += table.num_rows
                    buffered_bytes += table.nbytes
            flush()
        build = {
            "manifest_version": 1,
            "build_id": build_id,
            "collection_id": collection,
            "schema_version": SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
            "input_segments": sorted({s for b in builds for s in b["input_segments"]}),
            "input_checksums": {k: v for b in builds for k, v in b["input_checksums"].items()},
            "first_received_at": min(b["first_received_at"] for b in builds),
            "last_received_at": max(b["last_received_at"] for b in builds),
            "files": files,
            "supersedes": [b["build_id"] for b in builds],
            "committed_at": time.time_ns(),
            "logical_checksums": logical,
        }
        atomic_json(root / "builds" / build_id / "manifest.json", build)
        return {"compacted": len(builds), "build_id": build_id}
