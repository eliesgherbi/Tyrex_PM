"""Checksum-pinned teaching samples; no live connections or archive mutations.

This is not a qualified warehouse export. It verifies the normalized parts it
reads and preserves reception times/history. Raw archive integrity and settlement
are deliberately not attested by this teaching tool.
"""

from __future__ import annotations

import argparse
import json
from functools import cached_property
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from tyrex_pm.data_warehouse.capture.files import read_json, within
from tyrex_pm.data_warehouse.contracts import canonical, file_digest
from tyrex_pm.data_warehouse.normalize.schemas import schema
from tyrex_pm.data_warehouse.query.reader import BINANCE, Reader, exact_rows, order

NS = 1_000_000_000
VERSION = 1
DATASETS = (
    "markets",
    "instruments",
    "metadata_versions",
    "feeds",
    "feed_bindings",
    "book_snapshots",
    "book_updates",
    "quote_observations",
    "trade_prints",
    "reference_observations",
    "operational_events",
    "clock_observations",
    "market_changes",
    "resolution_observations",
)


def project_root() -> Path:
    for base in (Path.cwd(), Path(__file__).resolve().parent):
        for path in (base, *base.parents):
            if (path / "pyproject.toml").exists() and (path / "src/tyrex_pm").exists():
                return path
    raise FileNotFoundError("Set SAMPLE_PATH explicitly outside the checkout.")


def _selected_rows(reader: Reader, dataset: str, until: int, keys: dict) -> tuple[list, list]:
    """Filter Arrow batches before exact Decimal decoding; preserve reader ordering."""
    rows, inputs = [], []
    for build in reader.manifests:
        if dataset != "resolution_observations" and build["first_received_at"] > until:
            continue
        for file in build["files"]:
            if file["dataset"] != dataset:
                continue
            path = within(reader.root, file["path"])
            if file_digest(path) != file["sha256"]:
                raise ValueError(f"Normalized part checksum mismatch: {file['path']}")
            inputs.append({"path": file["path"], "sha256": file["sha256"]})
            for batch in pq.ParquetFile(path).iter_batches(batch_size=4096):
                table = pa.Table.from_batches([batch])
                mask = pc.less_equal(table["received_at"].cast(pa.int64()), until)
                if dataset == "resolution_observations":
                    mask = pc.equal(table["market_id"], keys["market"])
                elif dataset in ("markets", "instruments", "feed_bindings"):
                    mask = pc.and_(mask, pc.equal(table["market_id"], keys["market"]))
                elif dataset in ("metadata_versions", "market_changes"):
                    mask = pc.and_(
                        mask,
                        pc.is_in(
                            table["entity_id"], pa.array([keys["market"], *keys["instruments"]])
                        ),
                    )
                elif dataset in ("feeds", "reference_observations"):
                    mask = pc.and_(mask, pc.is_in(table["feed_id"], pa.array(keys["feeds"])))
                elif dataset in ("book_snapshots", "quote_observations", "trade_prints"):
                    wanted = keys["instruments"] + ([BINANCE] if dataset == "trade_prints" else [])
                    mask = pc.and_(mask, pc.is_in(table["instrument_id"], pa.array(wanted)))
                elif dataset == "book_updates":
                    mask = pc.and_(mask, pc.not_equal(table["source_id"], "binance"))
                if dataset in ("quote_observations", "trade_prints"):
                    mask = pc.and_(
                        mask, pc.greater_equal(table["received_at"].cast(pa.int64()), keys["start"])
                    )
                for row in exact_rows(table.filter(pc.fill_null(mask, False))):
                    if dataset == "book_updates":
                        row["changes"] = [
                            c for c in row["changes"] if c["instrument_id"] in keys["instruments"]
                        ]
                        if not row["changes"]:
                            continue
                    rows.append(row)
    rows.sort(key=order)
    return rows, inputs


def prepare_sample(root: Path, collection: str, output: Path, market: str | None = None) -> dict:
    """Extract one finished BTC5m window, retaining all earlier CLOB seed history.

    Reads only; requires a new output path. Default selection is chronological,
    never selected on price returns. Sparse/invalid observations remain visible.
    """
    if output.exists():
        raise FileExistsError(f"Use a new sample directory: {output}")
    explicit = market is not None
    reader = Reader(root, collection)
    if not reader.manifests:
        raise ValueError("No committed normalized builds. Normalize the collection first.")
    first = min(b["first_received_at"] for b in reader.manifests)
    last = max(b["last_received_at"] for b in reader.manifests)
    eligible = {
        m["market_id"]
        for m in reader.list_markets()
        if m["underlying_asset"] == "BTC" and m["duration_s"] == 300
    }
    latest = {}
    for row in reader.iter_rows("metadata_versions"):
        if row["entity_id"] in eligible and row["window_start"] and row["window_end"]:
            latest[row["entity_id"]] = row
    candidates = sorted(
        (
            r
            for r in latest.values()
            if r["window_end"] <= last
            and (r["entity_id"] == market if explicit else r["window_start"] >= first + 900 * NS)
        ),
        key=lambda r: (r["window_start"], r["entity_id"]),
    )
    if not candidates:
        raise ValueError("No finished BTC5m window with 15-minute lead-in; specify --market.")
    chosen = candidates[0]
    market, end = chosen["entity_id"], chosen["window_end"]
    instruments = sorted(
        {
            r["instrument_id"]
            for r in reader.iter_rows("instruments", until=end)
            if r["market_id"] == market
        }
    )
    if len(instruments) != 2:
        raise ValueError("Expected exactly two recorded outcome instruments.")
    feeds = sorted(
        {
            r["feed_id"]
            for r in reader.iter_rows("feed_bindings", until=end)
            if r["market_id"] == market
        }
    )
    keys = {
        "market": market,
        "instruments": instruments,
        "feeds": feeds,
        "start": chosen["window_start"],
    }
    output.mkdir(parents=True)
    files, inputs = [], {}
    for dataset in DATASETS:
        rows, source_files = _selected_rows(reader, dataset, end, keys)
        name = f"{dataset}.parquet"
        pq.write_table(
            pa.Table.from_pylist(rows, schema=schema(dataset)), output / name, compression="zstd"
        )
        files.append(
            {
                "dataset": dataset,
                "path": name,
                "rows": len(rows),
                "sha256": file_digest(output / name),
            }
        )
        inputs.update({item["path"]: item for item in source_files})
        print(f"{dataset}: {len(rows):,} rows", flush=True)
    quality, quality_inputs = {}, []
    status = root / f"collections/{collection}/quality_status.json"
    if status.exists():
        publication = read_json(status)
        for file in publication.get("files", []):
            if file["dataset"] != "market_coverage":
                continue
            path = within(root, file["path"])
            if file_digest(path) != file["sha256"]:
                raise ValueError("Published quality checksum mismatch")
            for row in exact_rows(pq.read_table(path)):
                if row["market_id"] == market:
                    quality = json.loads(row["report"])
                    quality_inputs.append({"path": file["path"], "sha256": file["sha256"]})
    (output / "quality.json").write_text(canonical(quality), encoding="utf-8")
    manifest = {
        "tutorial_sample_version": VERSION,
        "collection_id": collection,
        "market_id": market,
        "window_start": chosen["window_start"],
        "window_end": end,
        "history_from": first,
        "instrument_ids": instruments,
        "files": files,
        "source_build_ids": [b["build_id"] for b in reader.manifests],
        "source_parts": sorted(inputs.values(), key=lambda x: x["path"]),
        "quality_sources": quality_inputs,
        "quality_sha256": file_digest(output / "quality.json"),
        "integrity_scope": "selected_normalized_parts_and_sample_only_not_raw_archive",
        "sample_purpose": "descriptive_teaching_not_strategy_qualification",
        "label_policy": "resolution_observations_are_retrospective_only",
        "selection": "explicit_market" if explicit else "first_finished_after_15m_lead_in",
    }
    (output / "manifest.json").write_text(canonical(manifest), encoding="utf-8")
    return manifest


class Sample:
    """Verified input with lazy exact records and separate teaching/plotting views."""

    def __init__(self, path: Path, expected_sha256: str | None = None):
        self.path = path.resolve()
        self.manifest_sha256 = file_digest(self.path / "manifest.json")
        if expected_sha256 and self.manifest_sha256 != expected_sha256:
            raise ValueError("Tutorial manifest checksum mismatch")
        self.manifest = read_json(self.path / "manifest.json")
        if self.manifest.get("tutorial_sample_version") != VERSION:
            raise ValueError("Unsupported tutorial sample version")
        for file in self.manifest["files"]:
            if file_digest(within(self.path, file["path"])) != file["sha256"]:
                raise ValueError(f"Tutorial file checksum mismatch: {file['path']}")
        if file_digest(self.path / "quality.json") != self.manifest["quality_sha256"]:
            raise ValueError("Tutorial quality checksum mismatch")
        self.quality = read_json(self.path / "quality.json")
        self._records = {}

    @classmethod
    def open(cls, path: str | Path | None = None) -> Sample:
        expected = None
        if path is None:
            settings = read_json(Path(__file__).with_name("sample_settings.json"))
            path = project_root() / settings["sample_path"]
            expected = settings.get("manifest_sha256")
        return cls(Path(path), expected)

    def records(self, dataset: str) -> list[dict]:
        if dataset not in self._records:
            file = next(f for f in self.manifest["files"] if f["dataset"] == dataset)
            self._records[dataset] = exact_rows(pq.read_table(within(self.path, file["path"])))
        return self._records[dataset]

    def frame(self, dataset: str):
        """Prices stay Decimal objects; time columns become timezone-aware UTC."""
        import pandas as pd

        rows = self.records(dataset)
        result = pd.DataFrame(rows, columns=schema(dataset).names)
        for field in schema(dataset):
            if pa.types.is_timestamp(field.type):
                values = pd.array([r.get(field.name) for r in rows], dtype="Int64")
                result[field.name] = pd.to_datetime(values, unit="ns", utc=True)
        return result

    @cached_property
    def labels(self) -> dict:
        return {
            r["instrument_id"]: r["venue_label"] or r["normalized_label"]
            for r in self.records("instruments")
        }

    @property
    def start(self) -> int:
        return self.manifest["window_start"]

    @property
    def end(self) -> int:
        return self.manifest["window_end"]

    def describe(self) -> dict:
        import pandas as pd

        return {
            "collection": self.manifest["collection_id"],
            "market": self.manifest["market_id"],
            "start_utc": pd.Timestamp(self.start, tz="UTC"),
            "end_utc": pd.Timestamp(self.end, tz="UTC"),
            "manifest_sha256": self.manifest_sha256,
            "integrity_scope": self.manifest["integrity_scope"],
            "semantic_readiness": self.quality.get("semantic_readiness", "NOT_REPORTED"),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--market")
    args = parser.parse_args()
    result = prepare_sample(args.root, args.collection, args.output, args.market)
    print(f"Prepared {result['market_id']} at {args.output}")
    print(f"Manifest SHA256: {file_digest(args.output / 'manifest.json')}")


if __name__ == "__main__":
    main()
