"""Atomic snapshot persistence for shadow restart recovery."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


class PersistenceError(RuntimeError):
    pass


@dataclass(frozen=True, kw_only=True)
class SnapshotMeta:
    schema_version: int
    run_id: str
    market_id: str
    runtime_mode: str
    config_fingerprint: str
    updated_at: str


class StateSnapshotStore:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def save(self, payload: dict[str, Any]) -> None:
        if int(payload.get("schema_version", 0)) != SCHEMA_VERSION:
            raise PersistenceError("invalid schema_version on save")
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.write_text(data + "\n", encoding="utf-8")
        os.replace(tmp, self._path)

    def load(
        self,
        *,
        expected_market_id: str,
        expected_config_fingerprint: str,
        expected_runtime_mode: str,
    ) -> dict[str, Any]:
        if not self._path.exists():
            raise PersistenceError("snapshot missing")
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PersistenceError("corrupted snapshot JSON") from exc
        if int(payload.get("schema_version", -1)) != SCHEMA_VERSION:
            raise PersistenceError("incompatible schema_version")
        if payload.get("market_id") != expected_market_id:
            raise PersistenceError("market_id mismatch")
        if payload.get("config_fingerprint") != expected_config_fingerprint:
            raise PersistenceError("config_fingerprint mismatch")
        if payload.get("runtime_mode") != expected_runtime_mode:
            raise PersistenceError("runtime_mode mismatch")
        return payload

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
