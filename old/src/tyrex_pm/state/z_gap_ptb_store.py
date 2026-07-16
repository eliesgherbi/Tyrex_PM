"""Persisted per-window PTB lock state for Z-Gap enforce (D2)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_Z_GAP_PTB_STATE_PATH = Path("var/state/z_gap_ptb_lock.json")


@dataclass
class ZGapPtbWindowRecord:
    market_id: str
    event_start_ts: float
    event_end_ts: float
    selected_source: str | None = None
    selected_k: str | None = None
    live_k: str | None = None
    log_k: str | None = None
    difference_bps: float | None = None
    live_boundary_lag_ms: float | None = None
    log_boundary_lag_ms: float | None = None
    usable: bool = False
    locked: bool = False
    block_reason: str | None = None
    mismatch: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ZGapPtbWindowRecord:
        return cls(
            market_id=str(raw.get("market_id") or ""),
            event_start_ts=float(raw.get("event_start_ts") or 0),
            event_end_ts=float(raw.get("event_end_ts") or 0),
            selected_source=raw.get("selected_source"),
            selected_k=raw.get("selected_k"),
            live_k=raw.get("live_k"),
            log_k=raw.get("log_k"),
            difference_bps=raw.get("difference_bps"),
            live_boundary_lag_ms=raw.get("live_boundary_lag_ms"),
            log_boundary_lag_ms=raw.get("log_boundary_lag_ms"),
            usable=bool(raw.get("usable")),
            locked=bool(raw.get("locked")),
            block_reason=raw.get("block_reason"),
            mismatch=bool(raw.get("mismatch")),
        )


class ZGapPtbStore:
    """Single-file store keyed by market_id — one locked K per window."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_Z_GAP_PTB_STATE_PATH

    def load(self, market_id: str) -> ZGapPtbWindowRecord | None:
        data = self._read_all()
        raw = data.get(market_id)
        if not isinstance(raw, dict):
            return None
        return ZGapPtbWindowRecord.from_dict(raw)

    def save(self, record: ZGapPtbWindowRecord) -> None:
        data = self._read_all()
        existing = data.get(record.market_id)
        if isinstance(existing, dict) and existing.get("locked"):
            prev_k = str(existing.get("selected_k") or "")
            new_k = str(record.selected_k or "")
            if prev_k and new_k and prev_k != new_k:
                raise ValueError(
                    f"PTB lock violation for {record.market_id}: locked K={prev_k!r} cannot change to {new_k!r}"
                )
        data[record.market_id] = record.to_dict()
        self._write_all(data)

    def clear(self, market_id: str) -> None:
        data = self._read_all()
        data.pop(market_id, None)
        self._write_all(data)

    def _read_all(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_all(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
