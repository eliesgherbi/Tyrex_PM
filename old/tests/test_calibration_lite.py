"""Tests for calibration-lite report (A0.3)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from tyrex_pm.quant.volatility import SigmaConfig
from research.z_gap.calibration_lite import (
    build_eval_rows,
    brier_score,
    classify_status,
    load_btc_ticks,
    load_market_contexts,
    reliability_table,
    run_calibration_lite,
    MarketRecordingContext,
    EvalRow,
)

UTC = timezone.utc


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _synthetic_recording(root: Path) -> None:
    market = "btc_5m_20260703_1200"
    start = 1_780_000_000.0
    end = start + 300.0
    k = "100000"
    _write_jsonl(
        root / market / "events-00001.jsonl",
        [
            {
                "event_type": "price_to_beat_observed",
                "market_id": market,
                "payload": {
                    "event_start_ts": start,
                    "event_end_ts": end,
                    "price_to_beat": k,
                    "status": "observed",
                },
            },
            {
                "event_type": "best_bid_ask",
                "market_id": market,
                "payload": {"raw": {"best_bid": "0.54", "best_ask": "0.56"}},
            },
            {
                "event_type": "market_resolved",
                "market_id": market,
                "payload": {"raw": {"winning_outcome": "Up"}},
            },
        ],
    )
    btc_rows = []
    for i in range(30):
        ts = datetime.fromtimestamp(start + 10 + i, tz=UTC).isoformat()
        px = str(100000 + i * 5)
        btc_rows.append(
            {
                "event_type": "external_btc_tick",
                "market_id": "external/btc_binance",
                "source_ts": ts,
                "recv_ts": ts,
                "payload": {"stream": "bookTicker", "mid": px, "bid": px, "ask": px},
            }
        )
    _write_jsonl(root / "external" / "btc_binance" / "events-00001.jsonl", btc_rows)


def test_calibration_lite_runs_on_synthetic_fixture(tmp_path: Path) -> None:
    rec = tmp_path / "recording"
    _synthetic_recording(rec)
    out = tmp_path / "out"
    report = run_calibration_lite(
        input_dir=rec,
        output_dir=out,
        sigma_cfg=SigmaConfig(min_samples_s=5, sample_interval_s=1),
    )
    assert (out / "calibration_lite_report.json").is_file()
    assert (out / "calibration_lite_report.md").is_file()
    assert report.eval_row_count >= 5
    assert report.model_brier is not None
    assert len(report.reliability_table) >= 1
    assert report.pm_mid_brier != "unavailable"


def test_golden_day_insufficient_data(tmp_path: Path) -> None:
    golden = Path("tests/fixtures/recordings/golden_day")
    out = tmp_path / "out"
    report = run_calibration_lite(input_dir=golden, output_dir=out)
    assert report.calibration_lite_status == "insufficient_data"
    assert (out / "calibration_lite_report.json").is_file()


def test_brier_score_and_reliability_table() -> None:
    rows = [
        EvalRow("m", 1.0, 100.0, 0.9, 1, 0.8),
        EvalRow("m", 2.0, 90.0, 0.1, 0, 0.2),
        EvalRow("m", 3.0, 80.0, 0.8, 1, 0.7),
        EvalRow("m", 4.0, 70.0, 0.2, 0, 0.3),
        EvalRow("m", 5.0, 60.0, 0.6, 1, 0.55),
    ]
    score = brier_score([r.p_up for r in rows], [r.outcome_up for r in rows])
    assert 0 <= score <= 1
    table = reliability_table(rows, buckets=5)
    assert len(table) == 5


def test_classify_status_insufficient_data() -> None:
    status, _ = classify_status(row_count=2, model_brier=None, reliability=[])
    assert status == "insufficient_data"


def test_build_eval_rows_from_ticks() -> None:
    ticks = [(1_780_000_010.0 + i, Decimal(str(100000 + i))) for i in range(25)]
    markets = {
        "btc_5m_test": MarketRecordingContext(
            market_id="btc_5m_test",
            event_start_ts=1_780_000_000.0,
            event_end_ts=1_780_000_300.0,
            price_to_beat=Decimal("100000"),
            resolved_up=True,
            pm_mid=Decimal("0.55"),
        )
    }
    rows = build_eval_rows(
        ticks=ticks,
        markets=markets,
        sigma_cfg=SigmaConfig(min_samples_s=5, sample_interval_s=1),
    )
    assert len(rows) >= 5
