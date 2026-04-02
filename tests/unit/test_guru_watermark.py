"""Guru watermark store."""

from __future__ import annotations

from tyrex_pm.data.guru_watermark import GuruWatermarkStore, utc_now_ms


def test_cold_start_then_advance(tmp_path) -> None:
    p = tmp_path / "w.json"
    s = GuruWatermarkStore(p)
    s.load()
    assert s.last_seen_ts_ms is None
    s.ensure_initialized(backfill_seconds=0.0, now_ms=1_000_000)
    assert s.last_seen_ts_ms == 1_000_000
    s.advance(1_000_500)
    s2 = GuruWatermarkStore(p)
    s2.load()
    assert s2.last_seen_ts_ms == 1_000_500


def test_backfill_offsets_start(tmp_path) -> None:
    p = tmp_path / "w.json"
    s = GuruWatermarkStore(p)
    s.ensure_initialized(backfill_seconds=10.0, now_ms=100_000)
    assert s.last_seen_ts_ms == 100_000 - 10_000


def test_utc_now_ms_reasonable() -> None:
    assert utc_now_ms() > 1_700_000_000_000
