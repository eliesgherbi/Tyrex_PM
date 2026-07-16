"""Tests for dynamic PTB boundary gates."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from tyrex_pm.ingestion.price_to_beat_tracker import PTB_STATUS_OBSERVED, PTB_STATUS_OBSERVED_FROM_LOG
from tyrex_pm.runtime.z_gap_boundary_gate import (
    PTB_STATUS_INVALID,
    PTB_STATUS_LOCKED,
    PTB_STATUS_MISMATCH,
    PTB_STATUS_MISSING_GATE,
    PTB_STATUS_READY,
    evaluate_boundary_ptb_gate,
)
from tyrex_pm.runtime.z_gap_ptb_commissioning import COMMISSIONING_POLICY_NAME


def _write_tick(path: Path, *, source_ts: str, price: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"source_ts": source_ts, "recv_ts": source_ts, "price": price}) + "\n",
        encoding="utf-8",
    )


def test_boundary_locks_matching_live_and_log(tmp_path: Path) -> None:
    start = 1_700_000_000.0
    tick_path = tmp_path / "ticks.jsonl"
    _write_tick(
        tick_path,
        source_ts="2023-11-14T22:13:20+00:00",
        price="37234.12",
    )
    # Use event_start aligned to tick timestamp
    from datetime import datetime, timezone

    start = datetime.fromisoformat("2023-11-14T22:13:20+00:00").timestamp()
    result = evaluate_boundary_ptb_gate(
        market_id="btc_5m_20231114_2210",
        event_start_ts=start,
        event_end_ts=start + 300,
        now_ts=start + 1,
        live_price="37234.12",
        live_status=PTB_STATUS_OBSERVED,
        live_lag_ms=50.0,
        chainlink_log_path=tick_path,
        ptb_config_hash="abc123",
        artifacts_dir=tmp_path / "artifacts",
    )
    assert result.status == PTB_STATUS_READY
    assert result.ready_to_evaluate
    att = json.loads((tmp_path / "artifacts" / "ptb_attestation.json").read_text(encoding="utf-8"))
    assert att["attestation_pass"] is True
    assert att["K_derived"] == "37234.12"


def test_live_log_mismatch_blocks(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    start = datetime.fromisoformat("2023-11-14T22:13:20+00:00").timestamp()
    tick_path = tmp_path / "ticks.jsonl"
    _write_tick(tick_path, source_ts="2023-11-14T22:13:20+00:00", price="37234.12")
    result = evaluate_boundary_ptb_gate(
        market_id="btc_5m_20231114_2210",
        event_start_ts=start,
        event_end_ts=start + 300,
        now_ts=start + 1,
        live_price="37250.00",
        live_status=PTB_STATUS_OBSERVED,
        live_lag_ms=50.0,
        chainlink_log_path=tick_path,
        artifacts_dir=tmp_path / "artifacts",
    )
    assert result.status == PTB_STATUS_MISMATCH
    assert not result.ready_to_evaluate


def test_missing_k_blocks(tmp_path: Path) -> None:
    start = 1_700_000_000.0
    result = evaluate_boundary_ptb_gate(
        market_id="btc_5m_20231114_2210",
        event_start_ts=start,
        event_end_ts=start + 300,
        now_ts=start + 1,
        chainlink_log_path=tmp_path / "missing.jsonl",
        artifacts_dir=tmp_path / "artifacts",
    )
    assert result.status in {PTB_STATUS_MISSING_GATE, PTB_STATUS_INVALID}
    assert not result.ready_to_evaluate


def test_commissioning_certificate_allows_lock(tmp_path: Path) -> None:
    from datetime import datetime

    start = datetime.fromisoformat("2023-11-14T22:13:20+00:00").timestamp()
    cert = {
        "policy_name": COMMISSIONING_POLICY_NAME,
        "ptb_config_hash": "hash1",
        "issued_at_ts": start - 3600,
        "windows": [
            {"ptb_error_bps": 0.1},
            {"ptb_error_bps": 0.2},
            {"ptb_error_bps": 0.0},
        ],
    }
    art = tmp_path / "artifacts"
    art.mkdir(parents=True)
    (art / "ptb_commissioning_certificate.json").write_text(json.dumps(cert), encoding="utf-8")
    tick_path = tmp_path / "ticks.jsonl"
    _write_tick(tick_path, source_ts="2023-11-14T22:13:20+00:00", price="100000.00")
    result = evaluate_boundary_ptb_gate(
        market_id="btc_5m_20231114_2210",
        event_start_ts=start,
        event_end_ts=start + 300,
        now_ts=start + 1,
        live_price="100000.00",
        live_status=PTB_STATUS_OBSERVED,
        live_lag_ms=10.0,
        chainlink_log_path=tick_path,
        ptb_config_hash="hash1",
        artifacts_dir=art,
        ptb_store_path=tmp_path / "ptb_lock.json",
    )
    assert result.ready_to_evaluate
