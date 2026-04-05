"""Batched JSONL fact sink (REC-03, REC-04)."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tyrex_pm.reporting.schema.facts_v1 import FactValidationError, fact_envelope

_LOG = logging.getLogger(__name__)


def _iso_utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


class JsonlFactSink:
    __slots__ = (
        "_path",
        "_run_id",
        "_q",
        "_stop",
        "_thread",
        "_max_queue",
        "_batch_size",
        "_flush_interval_s",
        "_max_depth",
        "_dropped",
        "_flush_errors",
        "_started",
    )

    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        max_queue: int = 50_000,
        batch_size: int = 128,
        flush_interval_s: float = 0.05,
    ) -> None:
        self._path = path
        self._run_id = run_id
        self._q: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._max_queue = max_queue
        self._batch_size = max(1, batch_size)
        self._flush_interval_s = max(0.01, flush_interval_s)
        self._max_depth = 0
        self._dropped = 0
        self._flush_errors = 0
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._worker, name="tyrex_reporting_jsonl", daemon=True)
        self._thread.start()

    def emit_fact(self, fact_type: str, payload: dict[str, Any]) -> None:
        """Enqueue one validated fact row."""
        try:
            row = fact_envelope(
                fact_type=fact_type,
                run_id=self._run_id,
                recorded_at_utc=_iso_utc_now(),
                payload=payload,
            )
        except FactValidationError as exc:
            _LOG.warning("reporting validation drop fact_type=%s err=%s", fact_type, exc)
            self._dropped += 1
            return
        try:
            self._q.put_nowait(row)
        except queue.Full:
            self._dropped += 1
            _LOG.warning(
                "reporting queue full (max=%s); dropped fact_type=%s",
                self._max_queue,
                fact_type,
            )
            return
        depth = self._q.qsize()
        if depth > self._max_depth:
            self._max_depth = depth

    def _worker(self) -> None:
        batch: list[dict[str, Any]] = []
        last_flush = time.monotonic()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        while not self._stop.is_set() or not self._q.empty() or batch:
            try:
                timeout = max(0.0, self._flush_interval_s - (time.monotonic() - last_flush))
                item = self._q.get(timeout=timeout if batch else 0.2)
            except queue.Empty:
                item = None
            if item is None:
                if batch and (time.monotonic() - last_flush >= self._flush_interval_s or self._stop.is_set()):
                    self._flush_batch(batch)
                    batch.clear()
                    last_flush = time.monotonic()
                if self._stop.is_set() and self._q.empty():
                    break
                continue
            batch.append(item)
            if len(batch) >= self._batch_size or time.monotonic() - last_flush >= self._flush_interval_s:
                self._flush_batch(batch)
                batch.clear()
                last_flush = time.monotonic()

    def _flush_batch(self, batch: list[dict[str, Any]]) -> None:
        if not batch:
            return
        try:
            with self._path.open("a", encoding="utf-8") as f:
                for row in batch:
                    f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                f.flush()
        except OSError as exc:
            self._flush_errors += 1
            _LOG.error("reporting jsonl flush failed: %s", exc)

    def drain_and_close(self) -> dict[str, Any]:
        """Stop worker after draining; append final pipeline_health line; return stats."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30.0)
        stats = {
            "facts_dropped": self._dropped,
            "flush_errors": self._flush_errors,
            "queue_high_water": self._max_depth,
            "flush_ok": self._flush_errors == 0,
        }
        try:
            health = fact_envelope(
                fact_type="report_pipeline_health",
                run_id=self._run_id,
                recorded_at_utc=_iso_utc_now(),
                payload={
                    "flush_ok": stats["flush_ok"],
                    "facts_dropped": stats["facts_dropped"],
                    "flush_errors": stats["flush_errors"],
                    "queue_high_water": stats["queue_high_water"],
                },
            )
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(health, ensure_ascii=False, default=str) + "\n")
                f.flush()
        except OSError as exc:
            _LOG.error("reporting final health write failed: %s", exc)
        return stats
