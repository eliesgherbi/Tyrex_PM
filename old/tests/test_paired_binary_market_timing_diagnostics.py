"""BTC 5m market timing diagnostics (observability only)."""

from __future__ import annotations

import json

from tyrex_pm.core.ids import RunId
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.strategies.paired_binary import facts as pb_facts
from tyrex_pm.strategies.paired_binary.market_timing import (
    MARKET_TIMING_ACTIVE,
    MARKET_TIMING_CLOSED,
    MARKET_TIMING_NEAR_CLOSE,
    MARKET_TIMING_PRE_START,
    MARKET_TIMING_UNKNOWN,
    TIMING_SOURCE_SCENARIO,
    TIMING_SOURCE_UNKNOWN,
    build_market_timing_snapshot,
    classify_market_timing_phase,
)


def test_pre_start_phase() -> None:
    start = 1_000.0
    end = 1_300.0
    phase, to_start, to_close = classify_market_timing_phase(
        now_ts=900.0,
        event_start_ts=start,
        event_end_ts=end,
        near_close_window_s=45.0,
    )
    assert phase == MARKET_TIMING_PRE_START
    assert to_start == 100.0
    assert to_close == 400.0


def test_active_phase() -> None:
    start = 1_000.0
    end = 1_300.0
    phase, _, _ = classify_market_timing_phase(
        now_ts=1_100.0,
        event_start_ts=start,
        event_end_ts=end,
        near_close_window_s=45.0,
    )
    assert phase == MARKET_TIMING_ACTIVE


def test_near_close_phase() -> None:
    start = 1_000.0
    end = 1_300.0
    phase, _, to_close = classify_market_timing_phase(
        now_ts=1_270.0,
        event_start_ts=start,
        event_end_ts=end,
        near_close_window_s=45.0,
    )
    assert phase == MARKET_TIMING_NEAR_CLOSE
    assert to_close == 30.0


def test_closed_phase() -> None:
    phase, _, _ = classify_market_timing_phase(
        now_ts=1_400.0,
        event_start_ts=1_000.0,
        event_end_ts=1_300.0,
        near_close_window_s=45.0,
    )
    assert phase == MARKET_TIMING_CLOSED


def test_missing_timestamps_unknown() -> None:
    phase, to_start, to_close = classify_market_timing_phase(
        now_ts=1_000.0,
        event_start_ts=None,
        event_end_ts=None,
        near_close_window_s=45.0,
    )
    assert phase == MARKET_TIMING_UNKNOWN
    assert to_start is None
    assert to_close is None


def test_emit_market_timing_fact_has_expected_fields(tmp_path) -> None:
    snap = build_market_timing_snapshot(
        market_id="btc-5m",
        yes_token_id="yes",
        no_token_id="no",
        condition_id="cond-1",
        event_start_ts=1_000.0,
        event_end_ts=1_300.0,
        near_close_window_s=45.0,
        now_ts=1_050.0,
    )
    assert snap.phase == MARKET_TIMING_ACTIVE
    assert snap.source == TIMING_SOURCE_SCENARIO

    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        pb_facts.emit_market_timing(sink, RunId("timing"), snap)
        row = json.loads(sink._path.read_text(encoding="utf-8").strip())

    assert row["fact_type"] == "paired_binary_market_timing"
    payload = row["payload"]
    for key in (
        "market_id",
        "condition_id",
        "yes_token_id",
        "no_token_id",
        "event_start_ts",
        "event_end_ts",
        "now_ts",
        "phase",
        "seconds_to_start",
        "seconds_to_close",
        "near_close_window_s",
        "source",
    ):
        assert key in payload
    assert payload["phase"] == MARKET_TIMING_ACTIVE
    assert payload["source"] == TIMING_SOURCE_SCENARIO


def test_build_snapshot_without_timestamps_is_unknown() -> None:
    snap = build_market_timing_snapshot(
        market_id="btc-5m",
        yes_token_id="yes",
        no_token_id="no",
        condition_id=None,
        event_start_ts=None,
        event_end_ts=None,
        near_close_window_s=45.0,
        now_ts=1_000.0,
    )
    assert snap.phase == MARKET_TIMING_UNKNOWN
    assert snap.source == TIMING_SOURCE_UNKNOWN
