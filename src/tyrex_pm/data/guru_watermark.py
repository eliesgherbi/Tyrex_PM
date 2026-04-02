"""Persistent timestamp watermark for incremental guru activity polling."""

from __future__ import annotations

import json
import time
from pathlib import Path


def utc_now_ms() -> int:
    return int(time.time() * 1000)


class GuruWatermarkStore:
    """
    JSON file: ``{"last_seen_ts_ms": <int>}`` — millis since Unix epoch.

    Progress marker for “already covered” guru activity; polling requests only
    data at/after the API window derived from this value.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._last_seen_ts_ms: int | None = None

    @property
    def last_seen_ts_ms(self) -> int | None:
        return self._last_seen_ts_ms

    def load(self) -> None:
        if self._path is None or not self._path.is_file():
            self._last_seen_ts_ms = None
            return
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        v = raw.get("last_seen_ts_ms")
        self._last_seen_ts_ms = int(v) if v is not None else None

    def ensure_initialized(self, *, backfill_seconds: float, now_ms: int | None = None) -> None:
        """Cold start: watermark = now − backfill (no prior file state)."""
        if self._last_seen_ts_ms is not None:
            return
        now = now_ms if now_ms is not None else utc_now_ms()
        self._last_seen_ts_ms = now - int(max(0.0, backfill_seconds) * 1000)
        self.persist()

    def advance(self, new_ts_ms: int) -> None:
        """advance to at least ``new_ts_ms`` (typically max trade time in batch)."""
        cur = self._last_seen_ts_ms or 0
        if new_ts_ms > cur:
            self._last_seen_ts_ms = new_ts_ms
            self.persist()

    def persist(self) -> None:
        if self._path is None or self._last_seen_ts_ms is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"last_seen_ts_ms": self._last_seen_ts_ms}
        self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
