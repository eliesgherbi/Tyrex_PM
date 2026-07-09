"""Tests for research.lib.loaders (M2B.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pyarrow")

from research.lib.loaders import load_day, load_partitions
from research.normalize.run import normalize_day

GOLDEN_DAY = Path(__file__).resolve().parent / "fixtures" / "recordings" / "golden_day"


@pytest.fixture
def golden_parquet(tmp_path: Path) -> Path:
    out = tmp_path / "parquet"
    normalize_day(
        day_dir=GOLDEN_DAY,
        date="golden_day",
        out_root=out,
        market_id=None,
        include_external_btc=True,
        strict=False,
        overwrite=True,
    )
    return out


def test_load_day_reads_markets(golden_parquet: Path) -> None:
    day = load_day(golden_parquet / "date=golden_day", load_books=True)
    assert not day.tables["markets"].empty
    assert day.date == "golden_day"
    assert "btc_5m_20260703_1200" in day.book_snapshots


def test_load_partitions_concat_multi_day(golden_parquet: Path, tmp_path: Path) -> None:
    # Second partition: copy golden_day tables under another date label
    second = tmp_path / "parquet" / "date=golden_day_b"
    src = golden_parquet / "date=golden_day"
    second.mkdir(parents=True)
    for f in src.glob("*.parquet"):
        (second / f.name).write_bytes(f.read_bytes())
    merged = load_partitions([src, second], load_books=False)
    assert len(merged.tables["markets"]) == 2
    assert "golden_day+golden_day_b" == merged.date
