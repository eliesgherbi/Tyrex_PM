"""Golden normalization tests (M2B.3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pyarrow")

from research.normalize.run import normalize_day

GOLDEN_DAY = Path(__file__).resolve().parent / "fixtures" / "recordings" / "golden_day"


@pytest.fixture
def golden_out(tmp_path: Path) -> Path:
    return tmp_path / "parquet"


def test_normalize_golden_produces_parquet(golden_out: Path) -> None:
    summary = normalize_day(
        day_dir=GOLDEN_DAY,
        date="golden_day",
        out_root=golden_out,
        market_id=None,
        include_external_btc=True,
        strict=False,
        overwrite=True,
    )
    out_day = golden_out / "date=golden_day"
    assert (out_day / "markets.parquet").is_file()
    assert (out_day / "ws_quality.parquet").is_file()
    assert (out_day / "btc_ticks.parquet").is_file()
    assert (out_day / "clock_sync.parquet").is_file()
    assert (out_day / "trade_prices.parquet").is_file()
    assert (out_day / "tick_size_changes.parquet").is_file()
    assert (out_day / "best_bid_ask.parquet").is_file()
    assert (out_day / "market_resolutions.parquet").is_file()
    assert (out_day / "reference_prices.parquet").is_file()
    assert (out_day / "price_to_beat.parquet").is_file()
    assert (out_day / "market_id=btc_5m_20260703_1200" / "book_snapshots.parquet").is_file()
    assert (out_day / "market_id=btc_5m_20260703_1200" / "book_deltas.parquet").is_file()
    assert summary["market_count"] == 1
    assert summary["book_delta_rows"] >= 2
    assert summary["book_snapshot_rows"] >= 3
    assert summary["btc_tick_rows"] == 1
    assert summary["clock_sync_rows"] == 1
    assert summary["trade_price_rows"] == 1
    assert summary["tick_size_change_rows"] == 1
    assert summary["best_bid_ask_rows"] == 1
    assert summary["market_resolution_rows"] == 1
    assert summary["reference_price_rows"] == 1
    assert summary["price_to_beat_rows"] == 1


def _read_parquet(path: Path):
    import pyarrow.parquet as pq

    return pq.ParquetFile(path).read()


def test_markets_coverage_pct_populated(golden_out: Path) -> None:
    normalize_day(
        day_dir=GOLDEN_DAY,
        date="golden_day",
        out_root=golden_out,
        market_id=None,
        include_external_btc=True,
        strict=False,
        overwrite=True,
    )
    df = _read_parquet(golden_out / "date=golden_day" / "markets.parquet").to_pandas()
    assert len(df) == 1
    assert float(df.iloc[0]["coverage_pct"]) == 100.0
    assert df.iloc[0]["book_delta_count"] == 1
    assert df.iloc[0]["book_snapshot_count"] == 1
    assert df.iloc[0]["ws_seq_gap_count"] == 1
    assert float(df.iloc[0]["price_to_beat"]) == 109812.50
    assert df.iloc[0]["winning_outcome"] == "Up"
    assert int(df.iloc[0]["trade_price_count"]) == 1


def test_ws_quality_gap_rate(golden_out: Path) -> None:
    normalize_day(
        day_dir=GOLDEN_DAY,
        date="golden_day",
        out_root=golden_out,
        market_id=None,
        include_external_btc=False,
        strict=False,
        overwrite=True,
    )
    df = _read_parquet(golden_out / "date=golden_day" / "ws_quality.parquet").to_pandas()
    assert df.iloc[0]["ws_seq_gap_count"] == 1
    assert df.iloc[0]["gap_rate"] == pytest.approx(1 / 9)


def test_raw_json_preserved(golden_out: Path) -> None:
    normalize_day(
        day_dir=GOLDEN_DAY,
        date="golden_day",
        out_root=golden_out,
        market_id=None,
        include_external_btc=True,
        strict=False,
        overwrite=True,
    )
    snaps = _read_parquet(
        golden_out / "date=golden_day" / "market_id=btc_5m_20260703_1200" / "book_snapshots.parquet"
    ).to_pandas()
    assert snaps.iloc[0]["raw_json"]
    assert "book" in snaps.iloc[0]["raw_json"]
    ticks = _read_parquet(golden_out / "date=golden_day" / "btc_ticks.parquet").to_pandas()
    assert ticks.iloc[0]["raw_json"]


def test_non_strict_skips_corrupt_row(tmp_path: Path) -> None:
    day = tmp_path / "day"
    market = day / "btc_5m_test"
    market.mkdir(parents=True)
    (market / "events-00001.jsonl").write_text('{"bad": true}\n', encoding="utf-8")
    (market / "manifest.json").write_text(
        json.dumps(
            {
                "market_id": "btc_5m_test",
                "segments": [{"path": "events-00001.jsonl"}],
                "dropped_events": 0,
            }
        ),
        encoding="utf-8",
    )
    summary = normalize_day(
        day_dir=day,
        date="day",
        out_root=tmp_path / "out",
        market_id=None,
        include_external_btc=False,
        strict=False,
        overwrite=True,
    )
    assert summary["total_corrupt_rows"] == 1


def test_strict_raises_on_corrupt_row(tmp_path: Path) -> None:
    day = tmp_path / "day"
    market = day / "btc_5m_test"
    market.mkdir(parents=True)
    (market / "events-00001.jsonl").write_text('{"bad": true}\n', encoding="utf-8")
    (market / "manifest.json").write_text(
        json.dumps({"market_id": "btc_5m_test", "segments": [{"path": "events-00001.jsonl"}], "dropped_events": 0}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="corrupt"):
        normalize_day(
            day_dir=day,
            date="day",
            out_root=tmp_path / "out",
            market_id=None,
            include_external_btc=False,
            strict=True,
            overwrite=True,
        )


def test_zstd_segment_round_trip(tmp_path: Path) -> None:
    zstandard = pytest.importorskip("zstandard")

    from tyrex_pm.reporting.event_sink import read_event_segment_lines

    src_seg = GOLDEN_DAY / "btc_5m_20260703_1200" / "events-00001.jsonl"
    lines = read_event_segment_lines(src_seg)
    zst_path = tmp_path / "events-00001.jsonl.zst"
    cctx = zstandard.ZstdCompressor()
    zst_path.write_bytes(cctx.compress("\n".join(lines).encode("utf-8")))

    day = tmp_path / "zday"
    market = day / "btc_5m_z"
    market.mkdir(parents=True)
    (market / "events-00001.jsonl.zst").write_bytes(zst_path.read_bytes())
    (market / "manifest.json").write_text(
        json.dumps(
            {
                "market_id": "btc_5m_z",
                "yes_token_id": "y",
                "no_token_id": "n",
                "segments": [{"path": "events-00001.jsonl.zst", "event_count": len(lines)}],
                "dropped_events": 0,
                "compressed": True,
            }
        ),
        encoding="utf-8",
    )
    (day / "coverage_report.json").write_text(
        json.dumps(
            {
                "coverage_pct": 100.0,
                "markets": [{"market_id": "btc_5m_z", "status": "recorded"}],
            }
        ),
        encoding="utf-8",
    )
    normalize_day(
        day_dir=day,
        date="zday",
        out_root=tmp_path / "out",
        market_id=None,
        include_external_btc=False,
        strict=False,
        overwrite=True,
    )
    df = _read_parquet(tmp_path / "out" / "date=zday" / "markets.parquet").to_pandas()
    assert int(df.iloc[0]["book_delta_count"]) == 1
