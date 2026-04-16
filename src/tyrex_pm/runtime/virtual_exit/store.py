"""Atomic JSON persistence for virtual exit lots."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from tyrex_pm.runtime.virtual_exit.lot import ProtectedLot

_LOG = logging.getLogger(__name__)


class VirtualExitStore:
    """Load/save ``ProtectedLot`` rows to a single JSON file (atomic replace)."""

    __slots__ = ("_path",)

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load_lots(self) -> list[ProtectedLot]:
        if not self._path.is_file():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            _LOG.warning(
                "event=virtual_exit_store_corrupt path=%s err=%s — starting empty store",
                self._path,
                exc,
            )
            return []
        if not isinstance(raw, dict):
            return []
        rows = raw.get("lots")
        if not isinstance(rows, list):
            return []
        out: list[ProtectedLot] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                out.append(ProtectedLot.from_json_dict(row))
            except (TypeError, ValueError, KeyError) as exc:
                _LOG.warning("event=virtual_exit_store_row_skip err=%s row=%s", exc, row)
        return out

    def save_lots(self, lots: list[ProtectedLot]) -> None:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "lots": [lot.to_json_dict() for lot in lots],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, indent=2, sort_keys=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self._path)
