"""Offline quality publication; never runs on the capture event loop."""

from __future__ import annotations

import time
from contextlib import closing
from pathlib import Path

import pyarrow as pa

from ..capture.files import Ownership, atomic_json, read_json, within
from ..capture.writer import raw_manifests
from ..contracts import QUALITY_VERSION, file_digest, identity
from ..normalize.schemas import schema
from ..normalize.worker import write_table
from ..query.export import derived_rows
from ..query.reader import Reader
from ..reconstruction.policy import load_policy
from .coverage import coverage, nearby_references
from .index import CoverageIndex
from .readiness import readiness


def publish_quality(root: Path, collection: str) -> dict:
    with (
        Ownership(root, "quality-publisher"),
        Ownership(root, "quality-index"),
        closing(CoverageIndex(Reader(root, collection))) as reader,
    ):
        build_ids = sorted(b["build_id"] for b in reader.manifests)
        config = read_json(within(root, f"collections/{collection}/collection.json"))
        policy = load_policy(root, collection)
        raw_inputs = sorted(r["segment_id"] for r in raw_manifests(root, collection))
        normalized_segments = {s for b in reader.manifests for s in b["input_segments"]}
        normalization_complete = bool(raw_inputs) and set(raw_inputs) <= normalized_segments
        publication = identity(build_ids, raw_inputs, config, policy, QUALITY_VERSION)
        folder = within(root, f"collections/{collection}/quality/{publication[:24]}")
        manifest_path = folder / "manifest.json"
        if manifest_path.exists():
            existing = read_json(manifest_path)
            if existing["publication_id"] != publication:
                raise ValueError("quality publication path collision")
            atomic_json(within(root, f"collections/{collection}/quality_status.json"), existing)
            return existing
        progress_path = within(root, f"collections/{collection}/quality_progress.json")

        def indexing(done, total):
            atomic_json(
                progress_path,
                {
                    "stage": "indexing",
                    "parts_done": done,
                    "parts_total": total,
                    "updated_at": time.time_ns(),
                },
            )

        reader.update(indexing)
        reports, files = [], []
        metadata, resolutions = {}, {}
        windows = {}
        mappings = {}
        for row in reader.iter_rows("instruments"):
            mappings.setdefault(row["market_id"], {})[row["instrument_id"]] = row.get(
                "outcome_index"
            )
        for row in reader.iter_rows("metadata_versions"):
            metadata.setdefault(row["entity_id"], []).append(row)
            if row.get("window_start") is not None:
                windows[row["entity_id"]] = row
        for row in reader.iter_rows("resolution_observations"):
            resolutions.setdefault(row["market_id"], []).append(row["record_id"])
        horizon = max((b["last_received_at"] for b in reader.manifests), default=0)

        def priority(market):
            end = windows.get(market["market_id"], {}).get("window_end")
            return (
                0 if end is not None and end <= horizon else 1,
                end or horizon,
                market["market_id"],
            )

        for market in sorted(reader.list_markets(), key=priority):
            mid = market["market_id"]
            history = metadata.get(mid, [])
            window = windows.get(mid, {})
            if (window.get("window_start") or 0) > horizon:
                reports.append(
                    {
                        "market_id": mid,
                        "assessed_at": horizon,
                        "semantic_readiness": "UNAVAILABLE",
                        "reason": "window_not_started",
                        "scheduled_end": window.get("window_end"),
                    }
                )
                continue
            closed = None
            for row in history:
                if row.get("closed") is True:
                    closed = closed or row["received_at"]
                elif row.get("closed") is False:
                    closed = None
            inputs = [
                b["build_id"]
                for b in reader.manifests
                if b["first_received_at"] <= min(horizon, window.get("window_end") or horizon)
            ]
            revision = identity(
                mid,
                inputs,
                [
                    (r.get("window_start"), r.get("window_end"), r.get("rule_fingerprint"))
                    for r in [window]
                ],
                closed,
                mappings.get(mid, {}),
                normalization_complete,
                sorted(
                    {
                        (r.get("rule_fingerprint"), r.get("window_start"), r.get("window_end"))
                        for r in history
                    },
                    key=str,
                ),
                [
                    r["record_id"]
                    for r in nearby_references(
                        reader,
                        [
                            target
                            for h in history
                            for target in (h.get("window_start"), h.get("window_end"))
                            if target is not None
                        ],
                    )
                ],
                resolutions.get(mid, []),
                config,
                policy,
                QUALITY_VERSION,
            )
            cache = within(root, f"collections/{collection}/quality_markets/{revision[:24]}")
            cached_manifest = cache / "manifest.json"
            if cached_manifest.exists():
                cached = read_json(cached_manifest)
                if cached["revision"] != revision:
                    raise ValueError("market quality path collision")
                if file_digest(cache / "report.json") != cached["report_sha256"]:
                    raise ValueError("cached quality report checksum mismatch")
                report = read_json(cache / "report.json")
                reports.append(report)
                files.extend(cached["files"])
                continue
            report = coverage(reader, mid)
            report["input_build_ids"] = build_ids
            report["normalization_complete"] = normalization_complete
            report["readiness"] = readiness(report)
            reports.append(report)
            market_files = []
            for dataset, rows in derived_rows(mid, report, report.get("assessed_at", 0)).items():
                relative = f"{dataset}.parquet"
                details = write_table(
                    cache / relative, pa.Table.from_pylist(rows, schema=schema(dataset))
                )
                market_files.append(
                    {
                        "path": (cache / relative).relative_to(root).as_posix(),
                        "dataset": dataset,
                        **details,
                    }
                )
            atomic_json(cache / "report.json", report)
            atomic_json(
                cached_manifest,
                {
                    "files": market_files,
                    "revision": revision,
                    "market_id": mid,
                    "report_sha256": file_digest(cache / "report.json"),
                },
            )
            files.extend(market_files)
            atomic_json(
                progress_path,
                {
                    "stage": "markets",
                    "market_id": mid,
                    "market_report": (cache / "report.json").relative_to(root).as_posix(),
                    "markets_done": len(reports),
                    "updated_at": time.time_ns(),
                },
            )
        atomic_json(folder / "reports.json", reports)
        result = {
            "publication_id": publication,
            "publication_path": manifest_path.relative_to(root).as_posix(),
            "collection_id": collection,
            "quality_policy_version": QUALITY_VERSION,
            "input_build_ids": build_ids,
            "sealed_raw_segment_ids": raw_inputs,
            "normalization_complete": normalization_complete,
            "profile_hash": config.get("profile_hash"),
            "provider_policy": policy,
            "files": files,
            "file_path_base": "warehouse_root",
            "reports_sha256": file_digest(folder / "reports.json"),
            "markets": len(reports),
            "published_at": time.time_ns(),
        }
        atomic_json(manifest_path, result)
        atomic_json(within(root, f"collections/{collection}/quality_status.json"), result)
        atomic_json(
            progress_path,
            {
                "stage": "complete",
                "publication_id": publication,
                "markets_done": len(reports),
                "updated_at": time.time_ns(),
            },
        )
        return result
