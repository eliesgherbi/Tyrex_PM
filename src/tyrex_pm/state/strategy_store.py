from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GuruWatermark:
    """
    Lexicographic ordering: (ts_ms, dedup_key).
    Monotonic: only advance when processing a row strictly greater than cursor.
    """

    ts_ms: int
    dedup_id: str

    def as_tuple(self) -> tuple[int, str]:
        return (self.ts_ms, self.dedup_id)


@dataclass
class StrategyStore:
    guru_seen_dedup: set[str] = field(default_factory=set)
    guru_watermark: GuruWatermark | None = None


_STRATEGY_STORE_JSON_VERSION = 1


def load_strategy_store(path: Path) -> StrategyStore:
    if not path.is_file():
        return StrategyStore()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return StrategyStore()
    wm: GuruWatermark | None = None
    raw_wm = data.get("guru_watermark")
    if isinstance(raw_wm, dict):
        wm = GuruWatermark(ts_ms=int(raw_wm["ts_ms"]), dedup_id=str(raw_wm["dedup_id"]))
    seen_raw = data.get("guru_seen_dedup") or []
    seen: set[str] = set(str(x) for x in seen_raw) if isinstance(seen_raw, list) else set()
    return StrategyStore(guru_seen_dedup=seen, guru_watermark=wm)


def save_strategy_store(path: Path, store: StrategyStore) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "version": _STRATEGY_STORE_JSON_VERSION,
        "guru_watermark": (
            {"ts_ms": store.guru_watermark.ts_ms, "dedup_id": store.guru_watermark.dedup_id}
            if store.guru_watermark
            else None
        ),
        "guru_seen_dedup": sorted(store.guru_seen_dedup),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
