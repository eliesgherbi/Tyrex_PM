"""Tests for scripts/preflight_binance_connectivity.py (A0.0.b)."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.preflight_binance_connectivity import (
    BinanceConnectivityReport,
    _recommendation,
    _write_report,
)


def test_recommendation_fail_when_not_ok() -> None:
    msg = _recommendation(ok=False, reconnect_ok=None)
    assert "enforce" in msg.lower()
    assert "substitute" in msg.lower()


def test_recommendation_pass_when_ok() -> None:
    msg = _recommendation(ok=True, reconnect_ok=True)
    assert "reachable" in msg.lower()
    assert "A0.2" in msg


def test_write_report_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "binance_connectivity.json"
    report = BinanceConnectivityReport(
        ok=True,
        symbol="BTCUSDT",
        streams=["bookTicker"],
        ws_url="wss://example",
        ws_base="wss://stream.binance.com:9443",
        checked_at_utc="2026-07-09T00:00:00+00:00",
        connect_ms=120.5,
        first_message_ms=45.2,
    )
    _write_report(path, report)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["ok"] is True
    assert loaded["symbol"] == "BTCUSDT"
