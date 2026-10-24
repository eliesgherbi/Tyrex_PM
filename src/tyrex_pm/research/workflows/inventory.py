"""Read-only warehouse inventory. A call here does not publish, normalize, or index."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tyrex_pm.data_warehouse.capture.files import read_json
from tyrex_pm.data_warehouse.capture.writer import raw_manifests
from tyrex_pm.data_warehouse.query.reader import Reader
from tyrex_pm.research.strategy_replay.labels import proxy_outcome
from tyrex_pm.research.strategy_replay.loader import WindowCatalog

from .artifacts import plan_digest, slice_page, write_json
from .contracts import WorkflowError
from .partition import reserved_market_ids

NS = 1_000_000_000
FIVE_MINUTES = 300 * NS
_CACHE: dict[tuple, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()
_FLIGHTS: dict[tuple, "_Flight"] = {}
TRACKS = (
    "recording",
    "normalization",
    "up_book",
    "down_book",
    "reference",
    "quality",
    "outcome",
)
COMPLETE_FEED = {
    "CAPTURED_NO_DETECTED_GAP",
    "NO_DETECTED_GAP_WITH_STATED_ASSURANCE",
    "UNKNOWN_CONTINUITY",
}


def _parse_time(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise WorkflowError(
            "invalid_time", "time bounds are UTC dates or instants", {"value": value}
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * NS)


def _track(state: str, reason: str, **extra: Any) -> dict[str, Any]:
    body = {"state": state, "reason": reason}
    body.update(extra)
    return body


def _feed_track(feed: dict | None) -> dict[str, Any]:
    if not feed:
        return _track("unassessed", "no_quality_publication")
    required = int(feed.get("required_duration_ns") or 0)
    usable = int(feed.get("usable_duration_ns") or 0)
    status = str(feed.get("status") or "")
    intervals = feed.get("intervals") or []
    extra = {
        "required_duration_ns": required,
        "usable_duration_ns": usable,
        "intervals": intervals,
        "feed_status": status,
        "partial": bool(required and 0 < usable < required),
    }
    if status == "NOT_YET_EXPECTED" or required == 0:
        return _track("not_expected", status or "not_expected", **extra)
    if usable == required and status in COMPLETE_FEED:
        return _track("present", status, **extra)
    if usable == 0:
        return _track("missing", status or "no_usable_interval", **extra)
    return _track("missing", "partial_coverage", **extra)


def _instruments(attrs: dict) -> tuple[dict[str, str], int | None, int | None]:
    mapping: dict[str, str] = {}
    up_index = down_index = None
    for item in attrs.get("instruments") or []:
        label = str(item.get("normalized_label") or "")
        instrument = str(item.get("instrument_id") or "")
        if label and instrument:
            mapping[label] = instrument
        if label == "UP" and item.get("outcome_index") is not None:
            up_index = int(item["outcome_index"])
        if label == "DOWN" and item.get("outcome_index") is not None:
            down_index = int(item["outcome_index"])
    return mapping, up_index, down_index


class _LabelView:
    def __init__(self, rows: list[dict], up_index: int | None, down_index: int | None) -> None:
        self.resolution_rows = rows
        self.up_outcome_index = up_index
        self.down_outcome_index = down_index


def classify_outcome(
    rows: list[dict], up_index: int | None, down_index: int | None
) -> dict[str, Any]:
    """Proxy label from recorded payouts. Reserved callers must not call this."""
    if up_index is None or down_index is None:
        return {
            "outcome": "unknown",
            "label_source": "unknown",
            "label_type": "unavailable",
            "reason": "outcome_index_unmapped",
        }
    label = proxy_outcome(_LabelView(rows, up_index, down_index))
    if label.winner in {"UP", "DOWN"} and label.label_type == "proxy_outcome":
        return {
            "outcome": label.winner.lower(),
            "label_source": "proxy",
            "label_type": label.label_type,
            "method": label.method,
            "reason": label.reason,
        }
    if label.reason.startswith("no_resolved_observation"):
        return {
            "outcome": "unresolved",
            "label_source": "unknown",
            "label_type": "unavailable",
            "reason": label.reason,
        }
    return {
        "outcome": "unknown",
        "label_source": "unknown",
        "label_type": label.label_type,
        "reason": label.reason,
    }


def provider_confirmed(report: dict | None) -> bool:
    """Attested settlement only. An unverified proxy payout is not confirmation."""
    if not report:
        return False
    assessment = report.get("resolution_assessment") or report.get("settlement") or {}
    if not isinstance(assessment, dict):
        return False
    attested = (
        assessment.get("officially_attested") is True
        or assessment.get("chain_finality") == "ATTESTED"
    )
    return attested and assessment.get("status") == "PROVIDER_TERMINAL"


def _assessed_key(report: dict) -> tuple[int, int]:
    value = report.get("assessed_at")
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        return (1, 0)


def _quality_track(report: dict) -> dict[str, Any]:
    semantic = str(report.get("semantic_readiness") or "")
    if semantic == "READY":
        state, reason = "present", "READY"
    elif semantic == "UNSUPPORTED":
        state, reason = "missing", "semantic_unsupported"
    elif semantic == "UNAVAILABLE":
        state, reason = "unknown", str(report.get("reason") or "UNAVAILABLE")
    else:
        state, reason = "unknown", semantic or "unpublished_verdict"
    return _track(
        state,
        reason,
        policy_version=report.get("policy_version"),
        assessed_at=report.get("assessed_at"),
        reconstruction=report.get("reconstruction_usability"),
    )


def _merge_publication_reports(root: Path, collection: str, reports: dict[str, dict]) -> None:
    status_path = root / "collections" / collection / "quality_status.json"
    if not status_path.is_file():
        return
    try:
        publication = str(read_json(status_path).get("publication_id") or "")
    except (OSError, ValueError):
        return
    path = root / "collections" / collection / "quality" / publication[:24] / "reports.json"
    if not publication or not path.is_file():
        return
    try:
        published = read_json(path)
    except (OSError, ValueError):
        return
    for report in published:
        market = str(report.get("market_id") or "")
        if not market or market in reports:
            continue
        reports[market] = {
            "market_id": market,
            "assessed_at": report.get("assessed_at"),
            "semantic_readiness": report.get("semantic_readiness"),
            "reason": report.get("reason"),
            "scheduled_end": report.get("scheduled_end"),
        }


def _quality_reports(root: Path, collection: str) -> tuple[dict[str, dict], str | None, int | None]:
    folder = root / "collections" / collection / "quality_markets"
    reports: dict[str, dict] = {}
    if folder.is_dir():
        for manifest_path in sorted(folder.glob("*/manifest.json")):
            try:
                manifest = read_json(manifest_path)
                report = read_json(manifest_path.parent / "report.json")
            except (OSError, ValueError):
                continue
            market = manifest.get("market_id") or report.get("market_id")
            if not market:
                continue
            key = str(market)
            current = reports.get(key)
            if current is None or _assessed_key(report) >= _assessed_key(current):
                reports[key] = report
    _merge_publication_reports(root, collection, reports)
    status_path = root / "collections" / collection / "quality_status.json"
    publication = None
    assessed = None
    if status_path.is_file():
        try:
            status = read_json(status_path)
        except (OSError, ValueError):
            status = {}
        publication = status.get("publication_id")
        assessed = status.get("published_at")
    return reports, publication, assessed


def _reference_series(root: Path, collection: str) -> str:
    path = root / "collections" / collection / "collection.json"
    if not path.is_file():
        return "official_reference"
    try:
        profile = read_json(path).get("profile") or {}
    except (OSError, ValueError):
        return "official_reference"
    reference = profile.get("reference") or {}
    if isinstance(reference, dict):
        return str(reference.get("series_id") or reference.get("source") or "official_reference")
    return "official_reference"


class _Flight:
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.value: dict[str, Any] | None = None
        self.error: BaseException | None = None


def _decode_object(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _price_to_beat_value(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return None
    if isinstance(value, dict):
        recorded = value.get("priceToBeat")
        if recorded not in (None, ""):
            return recorded
        for item in value.values():
            found = _price_to_beat_value(item, depth + 1)
            if found not in (None, ""):
                return found
        return None
    if isinstance(value, list):
        for item in value[:40]:
            found = _price_to_beat_value(item, depth + 1)
            if found not in (None, ""):
                return found
    return None


def _as_ns(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, datetime):
        return int(value.timestamp() * NS)
    return None


def price_to_beat_record(rows: list[dict], window_end_ns: int) -> dict[str, Any]:
    """Earliest stored priceToBeat.

    Provider metadata is additional_fields, not the family attributes.
    """
    earliest: tuple[int, dict[str, Any]] | None = None
    for row in rows:
        received = _as_ns(row.get("received_at"))
        for source, raw in (
            ("provider_metadata", row.get("additional_fields")),
            ("family_attributes", row.get("family_attributes")),
        ):
            found = _price_to_beat_value(_decode_object(raw))
            if found in (None, ""):
                continue
            candidate = {
                "value": str(found),
                "source": source,
                "received_at_ns": received,
            }
            rank = received if received is not None else 2**63 - 1
            if earliest is None or rank < earliest[0]:
                earliest = (rank, candidate)
            break
    if earliest is None:
        return {
            "value": None,
            "source": None,
            "received_at_ns": None,
            "status": "unavailable",
            "comparison": "not_compared",
        }
    record = earliest[1]
    received = record["received_at_ns"]
    if received is None:
        status = "recorded_time_unknown_not_used_at_decision"
    elif received >= window_end_ns:
        status = "recorded_after_close_not_used_at_decision"
    else:
        status = "recorded_before_close_not_used_at_decision"
    return {**record, "status": status, "comparison": "not_compared"}


def _window_row(
    *,
    warehouse: Path,
    collection: str,
    ref: Any,
    catalog: WindowCatalog,
    reports: dict[str, dict],
    publication_id: str | None,
    recording: dict[str, Any],
    normalization: dict[str, Any],
    reference_series: str,
    reserved: set[str],
    duplicate_collections: list[str],
) -> dict[str, Any]:
    gamma = catalog.gamma_rows(ref.market_id)
    attrs = {}
    if gamma:
        raw_attrs = gamma[-1].get("family_attributes") or "{}"
        attrs = json.loads(raw_attrs) if isinstance(raw_attrs, str) else dict(raw_attrs or {})
    mapping, up_index, down_index = _instruments(attrs)
    report = reports.get(ref.market_id)
    feeds = (report or {}).get("feeds") or {}
    up_feed = feeds.get(mapping.get("UP", ""))
    down_feed = feeds.get(mapping.get("DOWN", ""))
    reference_feed = feeds.get("official_reference")
    not_started = (report or {}).get("reason") == "window_not_started"
    if not_started:
        up_track = _track("missing", "window_not_started")
        down_track = _track("missing", "window_not_started")
        reference_track = _track("missing", "window_not_started")
    else:
        up_track = _feed_track(up_feed)
        down_track = _feed_track(down_feed)
        reference_track = _feed_track(reference_feed)
    if report is None:
        quality = _track("unassessed", "no_quality_publication")
        warmup = _track("unassessed", "no_quality_publication", required=True)
    elif not_started:
        quality = _quality_track(report)
        warmup = _track("missing", "window_not_started", required=True)
    else:
        quality = _quality_track(report)
        warmup = _feed_track(reference_feed)
        warmup["required"] = True
    withheld = ref.market_id in reserved
    if withheld:
        outcome = {
            "outcome": "withheld",
            "label_source": "withheld",
            "partition": "reserved",
            "reason": "reserved_evaluation",
        }
        outcome_track = _track("withheld", "reserved_evaluation")
    else:
        outcome = classify_outcome(
            list(catalog.resolutions.get(ref.market_id) or []), up_index, down_index
        )
        if provider_confirmed(report):
            outcome["label_source"] = "provider_confirmed"
            outcome["label_type"] = "provider_confirmed"
        state = {
            "up": "present",
            "down": "present",
            "unresolved": "missing",
            "unknown": "unknown",
            "withheld": "withheld",
        }[outcome["outcome"]]
        outcome_track = _track(state, outcome.get("reason") or outcome["outcome"])
    sessions = []
    session_dir = warehouse / "collections" / collection / "sessions"
    if session_dir.is_dir():
        for path in sorted(session_dir.glob("*.json")):
            try:
                sessions.append(read_json(path).get("session_id") or path.stem)
            except (OSError, ValueError):
                sessions.append(path.stem)
    beat = price_to_beat_record(catalog.gamma_rows(ref.market_id), ref.window_end_ns)
    return {
        "market_id": ref.market_id,
        "slug": ref.slug,
        "warehouse": str(warehouse),
        "collection": collection,
        "window_start_ns": ref.window_start_ns,
        "window_end_ns": ref.window_end_ns,
        "asset": "BTC" if "btc" in ref.slug.lower() else None,
        "duration_s": (ref.window_end_ns - ref.window_start_ns) // NS,
        "tracks": {
            "recording": recording,
            "normalization": normalization,
            "up_book": up_track,
            "down_book": down_track,
            "reference": reference_track,
            "quality": quality,
            "outcome": outcome_track,
        },
        "warmup": warmup,
        "outcome": outcome["outcome"],
        "label_source": outcome["label_source"],
        "label_type": outcome.get("label_type"),
        "label_method": outcome.get("method"),
        "partition": "reserved" if withheld else "development",
        "duplicate": len(duplicate_collections) > 1,
        "collections": duplicate_collections,
        "reference_series": reference_series,
        "publication_id": publication_id,
        "assessed_at": (report or {}).get("assessed_at"),
        "sessions": sessions,
        "session_attribution": "collection",
        "price_to_beat_recorded": beat["status"] != "unavailable",
        "price_to_beat_value": beat["value"],
        "price_to_beat_status": beat["status"],
        "price_to_beat_source": beat["source"],
        "price_to_beat_comparison": beat["comparison"],
        "inferred": False,
    }


def _inferred_slot(start_ns: int) -> dict[str, Any]:
    missing = _track("missing", "schedule_slot_not_recorded")
    absent = _track("not_expected", "unrecorded_schedule_slot")
    return {
        "market_id": None,
        "slug": None,
        "window_start_ns": start_ns,
        "window_end_ns": start_ns + FIVE_MINUTES,
        "inferred": True,
        "outcome": None,
        "label_source": None,
        "partition": "unrecorded",
        "tracks": {
            "recording": missing,
            "normalization": absent,
            "up_book": absent,
            "down_book": absent,
            "reference": absent,
            "quality": absent,
            "outcome": absent,
        },
        "warmup": _track("not_expected", "unrecorded_schedule_slot"),
    }


def _availability(row: dict[str, Any]) -> str:
    if row.get("inferred"):
        return "gap"
    tracks = row["tracks"]
    ready_keys = ("up_book", "down_book", "reference", "quality")
    if (
        all(tracks[key]["state"] == "present" for key in ready_keys)
        and row["warmup"]["state"] == "present"
    ):
        return "ready"
    if tracks["recording"]["state"] == "present" and tracks["normalization"]["state"] != "present":
        return "preparation"
    if tracks["quality"]["state"] == "unassessed" and tracks["recording"]["state"] == "present":
        return "preparation"
    return "gap"


def _quality_stamp(root: Path) -> tuple:
    collections = root / "collections"
    if not collections.is_dir():
        return (0, 0)
    statuses = []
    reports = 0
    for folder in collections.iterdir():
        if not folder.is_dir():
            continue
        status = folder / "quality_status.json"
        if status.is_file():
            statuses.append(status.stat().st_mtime_ns)
        markets = folder / "quality_markets"
        if markets.is_dir():
            reports += sum(1 for item in markets.iterdir() if item.is_dir())
    return (max(statuses, default=0), reports)


def _root_stamp(roots: list[Path]) -> tuple:
    stamps = []
    for root in roots:
        builds = root / "builds"
        if not builds.is_dir():
            stamps.append((str(root), 0, 0, _quality_stamp(root)))
            continue
        children = list(builds.iterdir())
        latest = max((item.stat().st_mtime_ns for item in children), default=0)
        quality_stamp = _quality_stamp(root)
        stamps.append((str(root), len(children), latest, quality_stamp))
    return tuple(stamps)


_WAREHOUSE_CACHE: dict[tuple, dict[str, Any]] = {}
INDEX_SCHEMA = "workflow_inventory_index_v1"


def _live_stamp(warehouse: Path) -> tuple | None:
    """While a warehouse is being written (a collector heartbeats into it, or builds keep
    arriving from normalization), refresh its index at most every 10 minutes."""
    import time as _time

    builds = warehouse / "builds"
    if builds.is_dir():
        try:
            if _time.time() - builds.stat().st_mtime < 60:
                return ("writing", int(_time.time() // 600))
        except OSError:
            pass

    for status_path in (warehouse / "collections").glob("*/status.json"):
        try:
            beat = int(read_json(status_path).get("heartbeat_at") or 0)
        except (OSError, ValueError):
            continue
        if beat and _time.time_ns() - beat < 60 * 10**9:
            return ("recording", int(_time.time() // 600))
    return None


def _scan_warehouse(
    warehouse: Path,
    wanted_collection: str | None,
    wanted_session: str | None,
    asset: str,
    duration: Any,
    reserved: set[str],
) -> dict[str, Any]:
    """One warehouse, cached in memory and on disk under its own build and quality stamp.

    A recording that is still writing changes only its own warehouse's stamp, so the other
    warehouses are not read again; a server restart reuses the saved index.
    """
    key = (
        INDEX_SCHEMA,
        str(warehouse.resolve()) if warehouse.exists() else str(warehouse),
        wanted_collection,
        wanted_session,
        asset,
        None if duration is None else int(duration),
        tuple(sorted(reserved)),
        _live_stamp(warehouse) or _root_stamp([warehouse]),
    )
    cached = _WAREHOUSE_CACHE.get(key)
    if cached is not None:
        return cached
    # Next to the warehouses it describes: var/warehouse_* -> var/research/cache/inventory.
    index_dir = warehouse.resolve().parent / "research" / "cache" / "inventory"
    path = index_dir / f"{plan_digest(list(key))}.json"
    if path.is_file():
        try:
            value = read_json(path)
            if value.get("schema") == INDEX_SCHEMA:
                _WAREHOUSE_CACHE[key] = value
                return value
        except (OSError, ValueError):
            pass
    value = {
        "schema": INDEX_SCHEMA,
        **_scan_all([warehouse], wanted_collection, wanted_session, asset, duration, reserved),
    }
    index_dir.mkdir(parents=True, exist_ok=True)
    write_json(path, value)
    _WAREHOUSE_CACHE[key] = value
    return value


def _scan(
    roots: list[Path],
    wanted_collection: str | None,
    wanted_session: str | None,
    asset: str,
    duration: Any,
    reserved: set[str],
) -> dict[str, Any]:
    # The index is per warehouse and filter-free for asset and duration; both are cheap to
    # apply here, so one saved index serves every request on that warehouse.
    parts = [
        _scan_warehouse(root, wanted_collection, wanted_session, "", None, reserved)
        for root in roots
    ]
    rows = [
        dict(row)
        for part in parts
        for row in part["rows"]
        if (duration is None or row.get("slug") is None or row.get("duration_s") == int(duration))
        and (
            not asset
            or row.get("slug") is None
            or asset in str(row.get("slug") or "").upper()
            or asset in str(row.get("market_id") or "").upper()
        )
    ]
    # The same market recorded in two warehouses is a duplicate across the whole view.
    seen: dict[str, list[str]] = {}
    for row in rows:
        for collection in row.get("collections") or [row.get("collection")]:
            names = seen.setdefault(str(row["market_id"]), [])
            if collection and collection not in names:
                names.append(collection)
    for row in rows:
        row["collections"] = seen[str(row["market_id"])]
        row["duplicate"] = len(row["collections"]) > 1
    return {
        "rows": rows,
        "unavailable": [item for part in parts for item in part["unavailable"]],
        "publications": [item for part in parts for item in part["publications"]],
        "build_ids": [item for part in parts for item in part["build_ids"]],
    }


def _scan_all(
    roots: list[Path],
    wanted_collection: str | None,
    wanted_session: str | None,
    asset: str,
    duration: Any,
    reserved: set[str],
) -> dict[str, Any]:
    unavailable: list[dict[str, str]] = []
    rows: list[dict[str, Any]] = []
    publications: list[str] = []
    build_ids: list[str] = []
    seen: dict[str, list[str]] = {}
    staged: list[tuple] = []
    for warehouse in roots:
        if not warehouse.is_dir():
            unavailable.append({"root": str(warehouse), "reason": "root_unavailable"})
            continue
        collections = warehouse / "collections"
        if not collections.is_dir():
            unavailable.append({"root": str(warehouse), "reason": "no_collections"})
            continue
        for folder in sorted(item for item in collections.iterdir() if item.is_dir()):
            if wanted_collection and folder.name != wanted_collection:
                continue
            if wanted_session:
                session_dir = folder / "sessions"
                names = []
                if session_dir.is_dir():
                    names = [path.stem for path in session_dir.glob("*.json")]
                if wanted_session not in names:
                    continue
            try:
                reader = Reader(warehouse, folder.name)
                catalog = WindowCatalog(reader)
                raw = raw_manifests(warehouse, folder.name)
            except (OSError, ValueError) as exc:
                unavailable.append(
                    {"root": str(warehouse), "collection": folder.name, "reason": str(exc)}
                )
                continue
            reports, publication, _published = _quality_reports(warehouse, folder.name)
            if publication:
                publications.append(str(publication))
            for build in reader.manifests:
                build_ids.append(str(build.get("build_id")))
            if raw:
                recording = _track("present", "sealed_raw_manifest", segments=len(raw))
            elif reader.manifests:
                recording = _track("missing", "normalized_without_raw_manifest")
            else:
                recording = _track("missing", "no_raw_manifest")
            normalization = (
                _track("present", "committed_build", builds=len(reader.manifests))
                if reader.manifests
                else _track("missing" if raw else "unassessed", "no_committed_build")
            )
            series = _reference_series(warehouse, folder.name)
            for ref in catalog.windows():
                seen.setdefault(ref.market_id, [])
                if folder.name not in seen[ref.market_id]:
                    seen[ref.market_id].append(folder.name)
                staged.append(
                    (
                        warehouse,
                        folder.name,
                        ref,
                        catalog,
                        reports,
                        publication,
                        recording,
                        normalization,
                        series,
                    )
                )
    for (
        warehouse,
        collection,
        ref,
        catalog,
        reports,
        publication,
        recording,
        normalization,
        series,
    ) in staged:
        if duration is not None and (ref.window_end_ns - ref.window_start_ns) // NS != int(
            duration
        ):
            continue
        if asset and asset not in ref.slug.upper() and asset not in ref.market_id.upper():
            continue
        rows.append(
            _window_row(
                warehouse=warehouse,
                collection=collection,
                ref=ref,
                catalog=catalog,
                reports=reports,
                publication_id=publication,
                recording=recording,
                normalization=normalization,
                reference_series=series,
                reserved=reserved,
                duplicate_collections=seen.get(ref.market_id, [collection]),
            )
        )
    for row in rows:
        row["availability"] = _availability(row)
    return {
        "rows": rows,
        "unavailable": unavailable,
        "publications": publications,
        "build_ids": build_ids,
    }


def capture_blocks(roots: list[Path]) -> list[dict[str, Any]]:
    blocks = []
    for warehouse in roots:
        collections = warehouse / "collections"
        if not collections.is_dir():
            continue
        for collection in sorted(item for item in collections.iterdir() if item.is_dir()):
            sessions = collection / "sessions"
            if not sessions.is_dir():
                continue
            for path in sorted(sessions.glob("*.json")):
                try:
                    body = read_json(path)
                except (OSError, ValueError):
                    continue
                started = body.get("started_at")
                stopped = body.get("stopped_at")
                # A session may go on polling winners after its capture; its windows end
                # at windows_until. A session that only polled winners has none.
                until = body.get("windows_until", stopped)
                if (
                    not isinstance(started, int)
                    or not isinstance(stopped, int)
                    or not isinstance(until, int)
                    or until <= started
                ):
                    continue
                session_id = str(body.get("session_id") or path.stem)
                blocks.append(
                    {
                        "block_id": f"{collection.name}:{session_id}",
                        "session_id": session_id,
                        "warehouse": str(warehouse),
                        "collection": collection.name,
                        "started_at_ns": started,
                        "stopped_at_ns": stopped,
                        "windows_until_ns": until,
                        "outcome_wait": body.get("outcome_wait"),
                        "capture_clean": body.get("capture_clean"),
                        "clean_stop": body.get("clean_stop"),
                        "derived_processing_complete": body.get("derived_processing_complete"),
                    }
                )
    return blocks


def _assign_blocks(rows: list[dict[str, Any]], blocks: list[dict[str, Any]]) -> None:
    for row in rows:
        start = row.get("window_start_ns")
        matched = None
        if isinstance(start, int) and not row.get("inferred"):
            for block in blocks:
                if block["warehouse"] != row.get("warehouse"):
                    continue
                if block["collection"] != row.get("collection"):
                    continue
                if block["started_at_ns"] <= start < block["windows_until_ns"]:
                    matched = block
                    break
        row["block_id"] = matched["block_id"] if matched else None
        # Set when the recording stopped before this window closed.
        end = row.get("window_end_ns")
        row["recording_cut_at_ns"] = (
            matched["windows_until_ns"]
            if matched and isinstance(end, int) and matched["windows_until_ns"] < end
            else None
        )


def _load_cached(
    cache_key: tuple,
    roots: list[Path],
    wanted_collection: str | None,
    wanted_session: str | None,
    asset: str,
    duration: Any,
    reserved: set[str],
) -> dict[str, Any]:
    """One warehouse scan serves every overlapping read. Later callers wait for it."""
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached is not None:
            return cached
        flight = _FLIGHTS.get(cache_key)
        if flight is None:
            flight = _Flight()
            _FLIGHTS[cache_key] = flight
            owner = True
        else:
            owner = False
    if not owner:
        flight.ready.wait()
        if flight.error is not None:
            raise flight.error
        if flight.value is None:
            raise WorkflowError("inventory_failed", "the warehouse read did not finish", {})
        return flight.value
    try:
        value = _scan(roots, wanted_collection, wanted_session, asset, duration, reserved)
    except BaseException as exc:
        flight.error = exc
        with _CACHE_LOCK:
            _FLIGHTS.pop(cache_key, None)
        flight.ready.set()
        raise
    flight.value = value
    with _CACHE_LOCK:
        _CACHE[cache_key] = value
        _FLIGHTS.pop(cache_key, None)
    flight.ready.set()
    return value


def discover(request: dict) -> dict[str, Any]:
    roots = [Path(item) for item in request.get("roots") or []]
    if not roots:
        raise WorkflowError("missing_field", "roots are required", {"field": "roots"})
    start = _parse_time(request.get("start_utc"))
    end = _parse_time(request.get("end_utc"))
    if start is not None and end is not None and start >= end:
        raise WorkflowError("invalid_time", "end_utc is exclusive and must be after start_utc", {})
    wanted_collection = request.get("collection")
    wanted_session = request.get("session_id")
    asset = (request.get("asset") or "").upper()
    duration = request.get("duration")
    reserved = reserved_market_ids(request)
    cache_key = (
        tuple(str(path.resolve()) if path.exists() else str(path) for path in roots),
        wanted_collection,
        wanted_session,
        asset,
        None if duration is None else int(duration),
        tuple(sorted(reserved)),
        _root_stamp(roots),
    )
    cached = _load_cached(
        cache_key,
        roots,
        wanted_collection,
        wanted_session,
        asset,
        duration,
        reserved,
    )
    unavailable = list(cached["unavailable"])
    publications = list(cached["publications"])
    build_ids = list(cached["build_ids"])
    rows = []
    for row in cached["rows"]:
        start_ns = int(row["window_start_ns"])
        if start is not None and start_ns < start:
            continue
        if end is not None and start_ns >= end:
            continue
        rows.append(row)
    if request.get("schedule") == "btc_updown_5m" and start is not None and end is not None:
        known = {row["window_start_ns"] for row in rows}
        cursor = start
        while cursor < end:
            if cursor not in known:
                slot = _inferred_slot(cursor)
                slot["warehouse"] = None
                slot["collection"] = wanted_collection
                rows.append(slot)
            cursor += FIVE_MINUTES
    elif request.get("schedule") not in (None, "", "btc_updown_5m"):
        raise WorkflowError(
            "unsupported_schedule",
            "only btc_updown_5m may infer unrecorded slots",
            {"schedule": request.get("schedule")},
        )
    _assign_blocks(rows, capture_blocks(roots))
    wanted_blocks = {str(item) for item in request.get("block_ids") or []}
    if wanted_blocks:
        rows = [row for row in rows if row.get("block_id") in wanted_blocks]
    rows.sort(
        key=lambda row: (
            row["window_start_ns"],
            row.get("market_id") or "",
            row.get("collection") or "",
        )
    )
    for row in rows:
        if not row.get("inferred"):
            row["availability"] = _availability(row)
    availability = request.get("availability") or "all"
    if availability != "all":
        rows = [
            row
            for row in rows
            if row.get("availability") == availability
            or row.get("inferred")
            and availability == "gap"
        ]
    fingerprint = plan_digest(
        {
            "builds": sorted(build_ids),
            "publications": sorted(set(publications)),
            "roots": [str(path) for path in roots],
        }
    )
    assessed = [row.get("assessed_at") for row in rows if row.get("assessed_at") is not None]
    span = _span(cached["rows"])
    return {
        "timezone": "UTC",
        "interval": "half_open_window_start",
        "windows": rows,
        "unavailable_roots": unavailable,
        "inventory_fingerprint": fingerprint,
        "publication_ids": sorted(set(publications)),
        "assessed_at": max(assessed) if assessed else None,
        "reserved_withheld": sum(1 for row in rows if row.get("outcome") == "withheld"),
        "span_start_utc": span["span_start_utc"],
        "span_end_utc": span["span_end_utc"],
    }


def _span(rows: list[dict[str, Any]]) -> dict[str, str | None]:
    recorded = [int(row["window_start_ns"]) for row in rows if row.get("market_id")]
    if not recorded:
        return {"span_start_utc": None, "span_end_utc": None}
    first, last = min(recorded), max(recorded)

    def day(ns: int) -> datetime:
        return datetime.fromtimestamp(ns / NS, timezone.utc)

    return {
        "span_start_utc": day(first).date().isoformat(),
        "span_end_utc": (day(last).date() + timedelta(days=1)).isoformat(),
    }


def library_blocks(request: dict, root: Path) -> dict[str, Any]:
    del root
    roots = [Path(item) for item in request.get("roots") or []]
    if not roots:
        raise WorkflowError("missing_field", "roots are required", {"field": "roots"})
    return {
        "blocks": capture_blocks(roots),
        "membership": "window_start_inside_capture",
        "gap_between_blocks_required": False,
    }


def library_windows(request: dict, root: Path) -> dict[str, Any]:
    del root
    found = discover(request)
    page, cursor = slice_page(found["windows"], request)
    counts = {
        name: 0
        for name in ("unknown", "unassessed", "unavailable", "withheld", "missing", "present")
    }
    for row in found["windows"]:
        for track in row.get("tracks", {}).values():
            state = track.get("state")
            if state in counts:
                counts[state] += 1
    return {
        "timezone": found["timezone"],
        "interval": found["interval"],
        "windows": page,
        "next_cursor": cursor,
        "unavailable_roots": found["unavailable_roots"],
        "inventory_fingerprint": found["inventory_fingerprint"],
        "publication_ids": found["publication_ids"],
        "assessed_at": found["assessed_at"],
        "track_counts": counts,
        "unknown_is_zero": False,
        "reserved_withheld": found["reserved_withheld"],
        "matched": len(found["windows"]),
        "span_start_utc": found["span_start_utc"],
        "span_end_utc": found["span_end_utc"],
    }


def library_coverage(request: dict, root: Path) -> dict[str, Any]:
    del root
    bucket_s = int(request.get("bucket_s") or 300)
    if bucket_s < 1:
        raise WorkflowError("invalid_page", "bucket_s must be positive", {})
    found = discover(request)
    buckets: dict[int, dict[str, int]] = {}
    for row in found["windows"]:
        start = int(row["window_start_ns"])
        key = start - (start % (bucket_s * NS))
        bucket = buckets.setdefault(
            key,
            {
                "start_ns": key,
                "windows": 0,
                "unknown": 0,
                "withheld": 0,
                "unassessed": 0,
                "missing": 0,
                "ready": 0,
                "preparation": 0,
                "gap": 0,
            },
        )
        bucket["windows"] += 1
        outcome = row.get("tracks", {}).get("outcome", {})
        state = outcome.get("state")
        if state in bucket:
            bucket[state] += 1
        availability = row.get("availability") or "gap"
        if availability in bucket:
            bucket[availability] += 1
    ordered = [buckets[key] for key in sorted(buckets)]
    page, cursor = slice_page(ordered, request)
    return {
        "timezone": "UTC",
        "interval": "half_open_window_start",
        "aggregation": "count_of_windows_by_start",
        "bucket_s": bucket_s,
        "buckets": page,
        "next_cursor": cursor,
        "unavailable_roots": found["unavailable_roots"],
        "inventory_fingerprint": found["inventory_fingerprint"],
        "publication_ids": found["publication_ids"],
        "assessed_at": found["assessed_at"],
        "denominator": "windows_in_scope",
    }


def library_window(request: dict, root: Path) -> dict[str, Any]:
    del root
    market = request.get("market_id")
    if not market:
        raise WorkflowError("missing_field", "market_id is required", {"field": "market_id"})
    found = discover(request)
    matches = [row for row in found["windows"] if row.get("market_id") == market]
    if not matches:
        raise WorkflowError(
            "not_found", "window is not in this inventory scope", {"market_id": market}
        )
    chosen = matches[0]
    return {
        "timezone": "UTC",
        "interval": "half_open_window_start",
        "window": chosen,
        "duplicates": matches,
        "unavailable_roots": found["unavailable_roots"],
        "inventory_fingerprint": found["inventory_fingerprint"],
        "publication_ids": found["publication_ids"],
        "assessed_at": chosen.get("assessed_at"),
        "book_updates_are_not_coverage": True,
    }
