"""Deduplication store for guru trade ids (dev persistence under `var/`)."""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path


class GuruDedupStore:
    """
    In-memory LRU of seen `source_trade_id` values with optional JSON persistence.

    File format: {"ids": ["...", "..."]} (approximate LRU — on load, order is preserved).
    """

    def __init__(self, path: Path | None, *, max_ids: int = 4000) -> None:
        self._path = path
        self._max = max_ids
        self._seen: OrderedDict[str, None] = OrderedDict()

    def load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        ids: Iterable[str] = raw.get("ids") or []
        for tid in ids:
            self._seen[tid] = None

    def is_new(self, trade_id: str) -> bool:
        return trade_id not in self._seen

    def remember(self, trade_id: str) -> None:
        self._seen[trade_id] = None
        self._seen.move_to_end(trade_id)
        while len(self._seen) > self._max:
            self._seen.popitem(last=False)
        self._persist()

    def _persist(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ids": list(self._seen.keys())}
        self._path.write_text(json.dumps(payload), encoding="utf-8")
