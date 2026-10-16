from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import pyarrow as pa

from ..capture.files import atomic_json, read_json, within
from ..capture.writer import raw_manifests, segment_records
from ..contracts import QUALITY_VERSION, canonical, digest, exact, identity
from ..normalize.schemas import DATASETS, schema
from ..normalize.worker import write_table
from ..quality.coverage import coverage, coverage_source
from ..quality.readiness import readiness
from ..reconstruction.books import Books
from .reader import Reader


def archive_status(
    root: Path, collection: str | None = None, *, reader: Reader | None = None
) -> dict:
    errors = []
    raw_count = 0
    manifests = raw_manifests(root, collection)
    raw_ids = set()
    for manifest in manifests:
        try:
            for row in segment_records(root, manifest):
                if row.raw_record_id in raw_ids:
                    errors.append(f"duplicate_raw_identity:{row.raw_record_id}")
                raw_ids.add(row.raw_record_id)
                raw_count += 1
        except (ValueError, OSError, RuntimeError) as exc:
            errors.append(f"segment:{manifest['segment_id']}:{type(exc).__name__}")
    if not manifests:
        errors.append("no_committed_raw_segments")
    reader = reader or Reader(root, collection)
    processed = set()
    try:
        for build in reader.manifests:
            for file in build["files"]:
                if digest(within(root, file["path"]).read_bytes()) != file["sha256"]:
                    errors.append(f"part_checksum:{file['path']}")
        for row in reader.iter_rows("processing_results"):
            processed.add(row["raw_record_id"])
    except (ValueError, OSError) as exc:
        errors.append(f"normalized:{type(exc).__name__}")
    missing = raw_ids - processed
    if processed - raw_ids:
        errors.append("normalized_records_without_raw_evidence")
    return {
        "integrity_ok": not errors,
        "errors": errors,
        "raw_records": raw_count,
        "processed_raw_records": len(processed),
        "normalization_pending_raw_records": len(missing),
        "normalization_complete": not missing,
        "unsealed_segments": len(list((root / "spool").rglob("*.open.jsonl"))),
    }


def apply_integrity(report: dict, archive: dict) -> dict:
    report["capture_integrity"] = "VERIFIED" if archive["integrity_ok"] else "FAILED"
    report["normalization_complete"] = archive["normalization_complete"]
    report["readiness"] = readiness(report)
    return report


def verify(
    root: Path,
    collection: str | None = None,
    *,
    require_ready: bool = False,
    market: str | None = None,
) -> dict:
    reader = Reader(root, collection)
    archive = archive_status(root, collection, reader=reader)
    reports = []
    if archive["integrity_ok"]:
        # One read-only source for every market; never builds or locks the index.
        with coverage_source(reader) as source:
            reports = [
                apply_integrity(coverage(source, m["market_id"]), archive)
                for m in reader.list_markets()
                if market is None or m["market_id"] == market
            ]
    if market and not reports:
        archive["errors"].append("requested_market_not_available")
        archive["integrity_ok"] = False
    quality_pass = bool(reports) and all(r["readiness"]["ready"] for r in reports)
    return {
        **archive,
        "market_coverage": reports,
        "quality_ok": quality_pass,
        "ok": archive["integrity_ok"] and (not require_ready or quality_pass),
    }


def dependency_start(reader: Reader, instruments: set[str], start: int) -> int:
    """Retain original seed/bridge evidence; never synthesize a venue snapshot."""
    state = Books()
    seed_times = {}
    connection_starts = {}
    for dataset, row in reader.events(
        ("book_snapshots", "book_updates", "operational_events"), start
    ):
        if dataset == "book_snapshots":
            seed_times[row["record_id"]] = row["received_at"]
        if row["source_id"] == "binance" and dataset in ("book_snapshots", "book_updates"):
            connection_starts.setdefault(
                (row["session_id"], row["connection_id"]), row["received_at"]
            )
        state.apply(dataset, row)
    beginning = start
    wanted = instruments | {"binance:spot:BTCUSDT"}
    for book in state.physical():  # every usable socket stream, not only the selected one
        iid = book.instrument_id
        if iid in wanted and book.valid:
            beginning = min(beginning, seed_times[book.seed_id])
            if iid == "binance:spot:BTCUSDT":
                beginning = min(
                    beginning,
                    connection_starts.get((book.session_id, book.connection_id), beginning),
                )
    return beginning


def export_market(
    reader: Reader, market: str, output: Path, *, warmup_s: int | None = None
) -> dict:
    if output.exists():
        raise ValueError("immutable export destination already exists")
    metadata = reader.get_market_history(market)
    windows = [r for r in metadata if r.get("window_start") is not None]
    if not windows:
        raise ValueError("market has no observed schedule")
    collections = {r["collection_id"] for r in windows}
    if len(collections) != 1:
        raise ValueError("select one collection before exporting a market recorded more than once")
    collection = next(iter(collections))
    reader = Reader(
        reader.root,
        collection,
        manifests=[b for b in reader.manifests if b["collection_id"] == collection],
    )
    saved_path = within(reader.root, f"collections/{collection}/collection.json")
    saved = read_json(saved_path) if saved_path.exists() else {}
    profile = saved.get("profile", {})
    if warmup_s is None:
        warmup_s = profile.get("coverage", {}).get("external_warmup_s", 900)
    start = min(r["window_start"] for r in windows) - warmup_s * 1_000_000_000
    horizon = max((b["last_received_at"] for b in reader.manifests), default=0)
    closed = [r["received_at"] for r in metadata if r.get("closed") is True]
    close = max(closed) if closed else max(r["window_end"] for r in windows)
    end = min(horizon, close + profile.get("coverage", {}).get("post_close_tail_s", 120) * 10**9)
    instruments = {
        r["instrument_id"] for r in reader.iter_rows("instruments") if r["market_id"] == market
    }
    bindings = [r for r in reader.iter_rows("feed_bindings") if r["market_id"] == market]
    feeds = {r["feed_id"] for r in bindings}
    history_start = dependency_start(reader, instruments, start)
    archive = archive_status(reader.root, collection, reader=reader)
    if not archive["integrity_ok"]:
        raise ValueError("cannot export an archive with failed integrity checks")
    temp = output.with_name(f".{output.name}.{uuid.uuid4().hex}.building")
    temp.mkdir(parents=True)
    files = []
    try:
        for dataset in DATASETS:
            if dataset == "processing_results":
                continue
            rows = []
            for row in reader.iter_rows(dataset):
                linked = (
                    row.get("market_id") == market
                    or row.get("entity_id") == market
                    or row.get("entity_id") in instruments  # token-level market changes
                    or row.get("instrument_id") in instruments
                    or row.get("feed_id") in feeds
                )
                structural = dataset in (
                    "markets",
                    "instruments",
                    "metadata_versions",
                    "feed_bindings",
                )
                labels = dataset == "resolution_observations"
                shared = row["source_id"] == "binance" or dataset == "operational_events"
                book_history = dataset in ("book_snapshots", "book_updates")
                operations = dataset == "operational_events"
                lower = history_start if book_history else start
                eligible = labels or (
                    row["received_at"] <= end
                    and (structural or operations or lower <= row["received_at"])
                )
                if (linked or shared) and eligible:
                    rows.append(row)
            if not rows:
                continue
            category = "labels" if dataset == "resolution_observations" else "observations"
            relative = f"{category}/{dataset}.parquet"
            details = write_table(
                temp / relative, pa.Table.from_pylist(rows, schema=schema(dataset))
            )
            files.append({"path": relative, "dataset": dataset, **details})
        quality = apply_integrity(coverage(reader, market, warmup_s=warmup_s), archive)
        quality["input_build_ids"] = sorted(b["build_id"] for b in reader.manifests)
        horizon = quality["assessed_at"]
        atomic_json(temp / "quality.json", quality)
        atomic_json(temp / "boundaries.json", quality["boundaries"])
        atomic_json(
            temp / "source_context.json",
            {
                "collection_id": collection,
                "effective_profile": profile,
                "profile_hash": saved.get("profile_hash"),
                "source_contract_version": saved.get("source_contract_version", "UNAVAILABLE"),
                "provider_policy": quality.get("provider_policy", {}),
                "archive_verification": archive,
                "expected_universe": [
                    r
                    for r in reader.iter_rows("operational_events")
                    if r.get("event_type") in ("projection", "projection_batch")
                ],
            },
        )
        for dataset, rows in derived_rows(market, quality, horizon).items():
            relative = f"derived/{dataset}.parquet"
            table = pa.Table.from_pylist(rows, schema=schema(dataset))
            details = write_table(temp / relative, table)
            files.append({"path": relative, "dataset": dataset, **details})
        manifest = {
            "manifest_version": 1,
            "export_id": output.name,
            "market_id": market,
            "schema_version": "1",
            "parser_version": "1",
            "projection_version": "exact-arrow-v1",
            "quality_policy_version": QUALITY_VERSION,
            "received_from": start,
            "dependency_received_from": history_start,
            "received_until": end,
            "ordering_policy": reader.merge_policy,
            "cross_session_clock_uncertainty": "NOT_ELIMINATED",
            "build_manifests": reader.manifests,
            "files": files,
            "created_at": end,
            "sidecars": {
                name: digest((temp / name).read_bytes())
                for name in ("quality.json", "boundaries.json", "source_context.json")
            },
            "labels_separate": True,
        }
        atomic_json(temp / "manifest.json", manifest)
        temp.rename(output)
        return manifest
    except BaseException:
        # Retain partial output for inspection; it has no committed destination manifest.
        raise


def project_row(dataset: str, row: dict) -> dict:
    names = [field.name for field in schema(dataset)]
    return {name: row.get(name) for name in names}


def derived_rows(market: str, quality: dict, horizon: int) -> dict[str, list[dict]]:
    settlement = quality.get("settlement") or {}
    payouts = settlement.get("payouts")
    assessment = {
        "assessment_id": identity(
            market, settlement.get("status"), settlement.get("input_record_ids") or []
        ),
        "market_id": market,
        "status": settlement.get("status"),
        "input_record_ids": settlement.get("input_record_ids") or [],
        "payouts": (
            [value if isinstance(value, Decimal) else exact(value) for value in payouts]
            if payouts
            else None
        ),
        "available_at": settlement.get("available_at"),
        "computed_at": horizon,
        "chain_finality": settlement.get("chain_finality") or "UNVERIFIED",
    }
    intervals = []
    for feed, report in quality.get("feeds", {}).items():
        cursor = report["required_from"]
        spans = []
        for left, right in report["intervals"]:
            if left > cursor:
                spans.append((cursor, left, "MISSING_OR_UNUSABLE"))
            spans.append(
                (
                    left,
                    right,
                    "UNKNOWN_CONTINUITY"
                    if report.get("unknown_continuity_duration_ns")
                    else "CAPTURED_NO_DETECTED_GAP",
                )
            )
            cursor = right
        if cursor < report["required_until"]:
            spans.append((cursor, report["required_until"], "MISSING_OR_UNUSABLE"))
        for left, right, status in spans:
            intervals.append(
                {
                    "interval_id": identity(market, feed, left, right, status, QUALITY_VERSION),
                    "market_id": market,
                    "feed_id": feed,
                    "start_at": left,
                    "end_at": right,
                    "status": status,
                    "required": report.get("required", True),
                    "policy_version": QUALITY_VERSION,
                    "assessed_at": horizon,
                    "input_build_ids": quality.get("input_build_ids", []),
                }
            )
    return {
        "coverage_intervals": intervals,
        "market_coverage": [
            {
                "assessment_id": identity(market, canonical(quality)),
                "market_id": market,
                "assessed_at": horizon,
                "policy_version": QUALITY_VERSION,
                "report": canonical(quality),
                "input_build_ids": quality.get("input_build_ids", []),
            }
        ],
        "boundary_observations": [
            project_row("boundary_observations", row) for row in quality.get("boundaries") or []
        ],
        "book_epochs": [
            project_row("book_epochs", row) for row in quality.get("book_epochs") or []
        ],
        "resolution_assessments": [project_row("resolution_assessments", assessment)],
    }


def load_export(path: Path) -> dict:
    root = path.parent if path.name == "manifest.json" else path
    manifest = read_json(root / "manifest.json")
    if manifest.get("manifest_version") != 1 or manifest.get("schema_version") != "1":
        raise ValueError("unsupported export")
    for file in manifest["files"]:
        if digest(within(root, file["path"]).read_bytes()) != file["sha256"]:
            raise ValueError("export checksum mismatch")
    for name, checksum in manifest["sidecars"].items():
        if digest(within(root, name).read_bytes()) != checksum:
            raise ValueError("export sidecar checksum mismatch")
    return manifest
