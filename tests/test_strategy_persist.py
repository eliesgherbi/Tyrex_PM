from __future__ import annotations

from pathlib import Path

from tyrex_pm.state.strategy_store import (
    GuruWatermark,
    StrategyStore,
    load_strategy_store,
    save_strategy_store,
)


def test_strategy_store_roundtrip(tmp_path: Path) -> None:
    s = StrategyStore()
    s.guru_seen_dedup.update({"x", "y"})
    s.guru_watermark = GuruWatermark(ts_ms=1700000000000, dedup_id="act-a")
    p = tmp_path / "guru.json"
    save_strategy_store(p, s)
    s2 = load_strategy_store(p)
    assert s2.guru_seen_dedup == {"x", "y"}
    assert s2.guru_watermark == GuruWatermark(ts_ms=1700000000000, dedup_id="act-a")


def test_missing_file_is_empty_store(tmp_path: Path) -> None:
    s = load_strategy_store(tmp_path / "nope.json")
    assert s.guru_watermark is None
    assert s.guru_seen_dedup == set()
