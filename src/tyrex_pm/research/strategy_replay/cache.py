"""Prepared-window and catalog caches for parameter sweeps (research only).

A prepared window is the loader's output before any strategy runs, so one entry serves
every strategy configuration and scenario. An entry is reused only when its key still
matches; any change to the inputs produces a new key and a rebuild, never a silently
reused entry. The key covers:

* loader and cache versions, warm-up/after range;
* collection identity, its effective profile hash and schema/parser versions;
* every committed build overlapping the window range (build id + part checksums);
* the market's identity, metadata and resolution rows from the catalog (these can
  arrive after the window, outside the range fingerprint).

Entries are zlib-compressed local pickles written by this module (about 11 MB per
window); only files it wrote are read.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import time
import zlib
from pathlib import Path

from tyrex_pm.data_warehouse.query.reader import Reader

from .loader import LOADER_VERSION, NS, WindowCatalog, WindowData, load_window

CACHE_VERSION = "prepared_window_cache_v2"
CATALOG_CACHE_VERSION = "window_catalog_cache_v3"


def _digest(payload: object) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def collection_identity(reader: Reader) -> dict:
    path = reader.root / "collections" / str(reader.collection) / "collection.json"
    meta = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {
        "collection_id": reader.collection,
        "profile_hash": meta.get("profile_hash"),
        "schema_version": meta.get("schema_version"),
        "parser_version": meta.get("parser_version"),
        "source_contract_version": meta.get("source_contract_version"),
    }


def _market_rows_digest(catalog: WindowCatalog, market_id: str) -> str:
    def ids(rows: list[dict]) -> list:
        return sorted((r.get("record_id"), r.get("received_at")) for r in rows)

    return _digest(
        {
            "metadata": ids(catalog.metadata.get(market_id, [])),
            "instruments": ids(catalog.instruments.get(market_id, [])),
            "resolutions": ids(catalog.resolutions.get(market_id, [])),
        }
    )


def _range_builds(reader: Reader, start: int, until: int) -> list:
    return sorted(
        (b.get("build_id"), tuple(sorted((f["path"], f.get("sha256")) for f in b["files"])))
        for b in reader.manifests
        if b["last_received_at"] >= start and b["first_received_at"] <= until
    )


def window_cache_key(
    catalog: WindowCatalog, market_id: str, *, warmup_s: float, after_s: float
) -> str:
    rows = catalog.gamma_rows(market_id)
    if not rows:
        raise ValueError(f"market {market_id} has no gamma window metadata")
    start = int(rows[-1]["window_start"]) - int(warmup_s * NS)
    until = int(rows[-1]["window_end"]) + int(after_s * NS)
    return _digest(
        {
            "cache": CACHE_VERSION,
            "loader": LOADER_VERSION,
            "collection": collection_identity(catalog.reader),
            "market_id": market_id,
            "warmup_s": warmup_s,
            "after_s": after_s,
            "range": [start, until],
            "builds": _range_builds(catalog.reader, start, until),
            "market_rows": _market_rows_digest(catalog, market_id),
        }
    )


def _atomic_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    data = zlib.compress(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL), 6)
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _read(path: Path, key: str) -> object | None:
    if not path.exists():
        return None
    try:
        stored_key, value = pickle.loads(zlib.decompress(path.read_bytes()))
    except Exception:  # noqa: BLE001 - a corrupt entry is rebuilt, never trusted
        return None
    return value if stored_key == key else None


class WindowCache:
    """Disk cache of prepared windows keyed by their full input identity."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.hits = 0
        self.misses = 0
        self.last_seconds = 0.0

    def path(self, catalog: WindowCatalog, market_id: str) -> Path:
        safe = market_id.replace(":", "_")
        return self.directory / str(catalog.reader.collection) / f"{safe}.pkl"

    def load(
        self,
        catalog: WindowCatalog,
        market_id: str,
        *,
        warmup_s: float = 300.0,
        after_s: float = 30.0,
    ) -> WindowData:
        started = time.perf_counter()
        key = window_cache_key(catalog, market_id, warmup_s=warmup_s, after_s=after_s)
        path = self.path(catalog, market_id)
        cached = _read(path, key)
        if isinstance(cached, WindowData):
            self.hits += 1
            self.last_seconds = time.perf_counter() - started
            return cached
        self.misses += 1
        window = load_window(catalog, market_id, warmup_s=warmup_s, after_s=after_s)
        _atomic_write(path, (key, window))
        self.last_seconds = time.perf_counter() - started
        return window


def cached_catalog(reader: Reader, directory: Path) -> WindowCatalog:
    """Catalog cached against the exact committed build set of the collection."""
    key = _digest(
        {
            "cache": CATALOG_CACHE_VERSION,
            "collection": collection_identity(reader),
            "builds": sorted(
                (b.get("build_id"), tuple(sorted(f.get("sha256") or "" for f in b["files"])))
                for b in reader.manifests
            ),
        }
    )
    path = directory / str(reader.collection) / "_catalog.pkl"
    cached = _read(path, key)
    if isinstance(cached, dict):
        catalog = WindowCatalog.__new__(WindowCatalog)
        catalog.reader = reader
        catalog.metadata = cached["metadata"]
        catalog.instruments = cached["instruments"]
        catalog.resolutions = cached["resolutions"]
        catalog.clock_observations = cached["clock_observations"]
        return catalog
    catalog = WindowCatalog(reader)
    _atomic_write(
        path,
        (
            key,
            {
                "metadata": catalog.metadata,
                "instruments": catalog.instruments,
                "resolutions": catalog.resolutions,
                "clock_observations": catalog.clock_observations,
            },
        ),
    )
    return catalog
