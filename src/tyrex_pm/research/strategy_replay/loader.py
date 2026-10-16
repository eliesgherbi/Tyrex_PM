"""Time-bounded loading of one recorded market window from the warehouse.

Reads only the rows a strategy replay needs, between ``window_start - warmup`` and
``window_end + after``, plus the market's identity and metadata rows received before
the end. Nothing here interprets settlement semantics; it records what was observed.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from tyrex_pm.data_warehouse.capture.files import within
from tyrex_pm.data_warehouse.contracts import decode, file_digest
from tyrex_pm.data_warehouse.query.reader import Reader, entity_rows, exact_rows, order

NS = 1_000_000_000
LOADER_VERSION = "strategy_window_loader_v2"

# Datasets merged into the replay timeline, in addition to the market's own rows.
TIMELINE_DATASETS = (
    "book_snapshots",
    "book_updates",
    "operational_events",
    "trade_prints",
    "reference_observations",
    "market_changes",
)


@dataclass(frozen=True)
class WindowRef:
    market_id: str
    slug: str
    window_start_ns: int
    window_end_ns: int


@dataclass
class WindowData:
    ref: WindowRef
    condition_id: str
    question: str
    rules_text: str | None
    resolution_source: str | None
    crypto_market_config: dict[str, Any] | None
    up_instrument_id: str
    down_instrument_id: str
    up_token_id: str
    down_token_id: str
    up_outcome_index: int
    down_outcome_index: int
    events: list[tuple[str, dict]]
    resolution_rows: list[dict]
    metadata_rows: list[dict]
    range_start_ns: int
    range_end_ns: int
    source_fingerprint: str
    load_seconds: float
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def token_by_instrument(self) -> dict[str, str]:
        return {
            self.up_instrument_id: self.up_token_id,
            self.down_instrument_id: self.down_token_id,
        }

    @property
    def instrument_by_token(self) -> dict[str, str]:
        return {token: iid for iid, token in self.token_by_instrument.items()}


class WindowCatalog:
    """Per-collection index of market identity rows, read once.

    ``Reader.get_market_history`` scans every metadata part per call; replaying many
    windows would rescan the collection hundreds of times. The catalog reads the
    metadata, instrument and resolution datasets once and serves them by market.
    """

    def __init__(self, reader: Reader) -> None:
        self.reader = reader
        self.metadata: dict[str, list[dict]] = {}
        self.instruments: dict[str, list[dict]] = {}
        self.resolutions: dict[str, list[dict]] = {}
        # Collector clock observations (offset vs reference server time), chronological.
        self.clock_observations: list[dict] = []
        for row in reader.iter_rows("metadata_versions"):
            if row.get("entity_type") == "market":
                self.metadata.setdefault(row["entity_id"], []).append(row)
        for row in reader.iter_rows("instruments"):
            self.instruments.setdefault(row.get("market_id") or "", []).append(row)
        for row in reader.iter_rows("resolution_observations"):
            self.resolutions.setdefault(row.get("market_id") or "", []).append(row)
        self.clock_observations = sorted(
            (r for r in reader.iter_rows("clock_observations") if r.get("status") == "OBSERVED"),
            key=lambda r: r["received_at"],
        )

    def clock_observations_for(self, start_ns: int, until_ns: int) -> list[dict]:
        """Observations in range plus the latest one received before it (known at start)."""
        before = [r for r in self.clock_observations if r["received_at"] < start_ns]
        inside = [r for r in self.clock_observations if start_ns <= r["received_at"] <= until_ns]
        return before[-1:] + inside

    def gamma_rows(self, market_id: str) -> list[dict]:
        return [
            row
            for row in self.metadata.get(market_id, [])
            if row.get("source_id") == "gamma" and row.get("window_start") is not None
        ]

    def windows(self) -> list[WindowRef]:
        """Every market window with gamma timing metadata, ordered by start time."""
        refs: list[WindowRef] = []
        for market_id in self.metadata:
            rows = self.gamma_rows(market_id)
            if not rows:
                continue
            last = rows[-1]
            attrs = decode(last.get("family_attributes") or "{}") or {}
            refs.append(
                WindowRef(
                    market_id=market_id,
                    slug=str(attrs.get("slug") or market_id),
                    window_start_ns=int(last["window_start"]),
                    window_end_ns=int(last["window_end"]),
                )
            )
        return sorted(refs, key=lambda r: (r.window_start_ns, r.market_id))


def list_windows(reader: Reader | WindowCatalog) -> list[WindowRef]:
    catalog = reader if isinstance(reader, WindowCatalog) else WindowCatalog(reader)
    return catalog.windows()


def _fingerprint(reader: Reader, start: int, until: int) -> str:
    """Identity of every committed build that can contain rows in [start, until]."""
    builds = sorted(
        (
            b.get("build_id") or "",
            b.get("first_received_at"),
            b.get("last_received_at"),
            tuple(sorted((f["path"], f.get("sha256") or "") for f in b["files"])),
        )
        for b in reader.manifests
        if b["last_received_at"] >= start and b["first_received_at"] <= until
    )
    payload = json.dumps(
        {"loader": LOADER_VERSION, "collection": reader.collection, "builds": builds},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _arrow_rows(
    reader: Reader, dataset: str, start: int, until: int, ours: set[str], market_id: str
):
    """Rows of ``dataset`` in [start, until] relevant to one market, filtered in Arrow.

    Same parts, checksum verification and row content as ``Reader.iter_rows``; the
    entity split of ``book_updates`` reuses the reader's own ``entity_rows``. Callers
    sort the combined timeline with the reader's ``order`` key.
    """
    wanted = pa.array(sorted(ours))
    for build in reader.manifests:
        if build["last_received_at"] < start or build["first_received_at"] > until:
            continue
        for file in build["files"]:
            if file["dataset"] != dataset:
                continue
            left = file.get("first_received_at", build["first_received_at"])
            right = file.get("last_received_at", build["last_received_at"])
            if left > until or right < start:
                continue
            path = within(reader.root, file["path"])
            if file_digest(path) != file["sha256"]:
                raise ValueError("normalized part checksum mismatch")
            table = pq.ParquetFile(path).read()  # file only: no hive partition columns
            received = table["received_at"].cast(pa.int64())
            mask = pc.and_(
                pc.greater_equal(received, start), pc.less_equal(received, until)
            )
            if dataset == "book_snapshots":
                mask = pc.and_(mask, pc.is_in(table["instrument_id"], wanted))
            elif dataset == "book_updates":
                flat = pc.struct_field(pc.list_flatten(table["changes"]), "instrument_id")
                parents = pc.list_parent_indices(table["changes"])
                hits = pc.unique(pc.filter(parents, pc.is_in(flat, wanted)))
                touched = pc.is_in(pa.array(range(len(table)), pa.int64()), hits.cast(pa.int64()))
                mask = pc.and_(mask, touched)
            elif dataset == "trade_prints":
                mask = pc.and_(
                    mask,
                    pc.or_(
                        pc.equal(table["source_id"], "binance"),
                        pc.is_in(table["instrument_id"], wanted),
                    ),
                )
            elif dataset == "market_changes":
                mask = pc.and_(
                    mask,
                    pc.is_in(table["entity_id"], pa.array([*sorted(ours), market_id])),
                )
            for row in exact_rows(table.filter(pc.fill_null(mask, False))):
                if dataset == "book_updates":
                    for entity, item in entity_rows(dataset, row):
                        if entity in ours:
                            yield item
                else:
                    yield row


def load_window(
    catalog: WindowCatalog,
    market_id: str,
    *,
    warmup_s: float = 300.0,
    after_s: float = 30.0,
    method: str = "arrow",
) -> WindowData:
    """Load one window. ``arrow`` filters before Decimal decoding; ``reader`` uses the
    warehouse Reader row by row. Both yield identical events (tested)."""
    started = time.perf_counter()
    reader = catalog.reader
    gamma = catalog.gamma_rows(market_id)
    if not gamma:
        raise ValueError(f"market {market_id} has no gamma window metadata")
    last = gamma[-1]
    attrs = decode(last.get("family_attributes") or "{}") or {}
    instruments = {i["normalized_label"]: i for i in attrs.get("instruments", [])}
    if set(instruments) != {"UP", "DOWN"}:
        raise ValueError(f"market {market_id} does not map exactly to UP/DOWN instruments")
    additional = decode(last.get("additional_fields") or "{}") or {}
    ref = WindowRef(
        market_id=market_id,
        slug=str(attrs.get("slug") or market_id),
        window_start_ns=int(last["window_start"]),
        window_end_ns=int(last["window_end"]),
    )
    start = ref.window_start_ns - int(warmup_s * NS)
    until = ref.window_end_ns + int(after_s * NS)
    up, down = instruments["UP"]["instrument_id"], instruments["DOWN"]["instrument_id"]
    ours = {up, down}

    events: list[tuple[str, dict]] = []
    counts: dict[str, int] = {}

    def add(dataset: str, rows) -> None:
        n = 0
        for row in rows:
            events.append((dataset, row))
            n += 1
        counts[dataset] = counts.get(dataset, 0) + n

    if method == "arrow":
        for dataset in TIMELINE_DATASETS:
            add(dataset, _arrow_rows(reader, dataset, start, until, ours, market_id))
    elif method == "reader":
        add("book_snapshots", reader.iter_rows("book_snapshots", start, until, entities=ours))
        add("book_updates", reader.iter_rows("book_updates", start, until, entities=ours))
        add("operational_events", reader.iter_rows("operational_events", start, until))
        add(
            "trade_prints",
            (
                r
                for r in reader.iter_rows("trade_prints", start, until)
                if r["source_id"] == "binance" or r.get("instrument_id") in ours
            ),
        )
        add("reference_observations", reader.iter_rows("reference_observations", start, until))
        add(
            "market_changes",
            (
                r
                for r in reader.iter_rows("market_changes", start, until)
                if r.get("entity_id") in ours or r.get("entity_id") == market_id
            ),
        )
    else:
        raise ValueError(f"unknown load method {method!r}")
    # Identity/metadata state: everything known before the end of the range.
    add(
        "instruments",
        (
            r
            for r in catalog.instruments.get(market_id, [])
            if r.get("instrument_id") in ours and r["received_at"] <= until
        ),
    )
    metadata = [r for r in catalog.metadata.get(market_id, []) if r["received_at"] <= until]
    before = [r for r in metadata if r["received_at"] < start]
    kept = before[-2:] + [r for r in metadata if r["received_at"] >= start]
    add("metadata_versions", kept)
    events.sort(key=lambda item: order(item[1]))
    resolution = list(catalog.resolutions.get(market_id, []))
    return WindowData(
        ref=ref,
        condition_id=str(attrs.get("condition_id") or last.get("condition_id") or ""),
        question=str(last.get("question") or ""),
        rules_text=last.get("rules_text"),
        resolution_source=last.get("resolution_source"),
        crypto_market_config=additional.get("cryptoMarketConfig"),
        up_instrument_id=up,
        down_instrument_id=down,
        up_token_id=str(instruments["UP"]["token_id"]),
        down_token_id=str(instruments["DOWN"]["token_id"]),
        up_outcome_index=int(instruments["UP"]["outcome_index"]),
        down_outcome_index=int(instruments["DOWN"]["outcome_index"]),
        events=events,
        resolution_rows=resolution,
        metadata_rows=kept,
        range_start_ns=start,
        range_end_ns=until,
        source_fingerprint=_fingerprint(reader, start, until),
        load_seconds=time.perf_counter() - started,
        counts=counts,
    )
