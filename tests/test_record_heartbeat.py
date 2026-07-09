"""Record heartbeat and graceful shutdown tests (M2B.1-B)."""

from __future__ import annotations

import json
import time

import pytest

from tyrex_pm.core.events import EventType, MarketEvent, compute_event_id, compute_payload_digest
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.reporting.event_sink import EventSink
from tyrex_pm.runtime.record_run import (
    MarketRecordState,
    RecordSessionState,
    _write_final_session_artifacts,
    build_coverage_report,
    build_heartbeat_payload,
    build_heartbeat_payload_from_manifests,
)


def _event(i: int = 0) -> MarketEvent:
    payload = {"raw": {"i": i}}
    digest = compute_payload_digest(payload)
    recv = utc_now()
    return MarketEvent(
        event_id=compute_event_id(
            event_type=EventType.BOOK_DELTA.value,
            token_id="tok",
            venue_cursor=f"h{i}",
            source_ts=None,
            payload_digest=digest,
        ),
        event_type=EventType.BOOK_DELTA,
        market_id="m1",
        token_id=TokenId("tok"),
        venue_cursor=f"h{i}",
        source_ts=None,
        recv_ts=recv,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_graceful_stop_sets_recording_ended_ts(tmp_path) -> None:
    sink = EventSink(tmp_path / "rec", "m1", batch_size=1, batch_flush_ms=1)
    await sink.start()
    sink.emit(_event())
    await sink.stop()
    assert sink.manifest.recording_ended_ts is not None


@pytest.mark.asyncio
async def test_heartbeat_payload_tracks_event_count(tmp_path) -> None:
    import asyncio

    sink = EventSink(tmp_path / "rec", "m1", batch_size=1, batch_flush_ms=1)
    session = RecordSessionState(recording_started_ts="2026-07-03T00:00:00+00:00")
    await sink.start()
    sink.emit(_event())
    await sink.stop()
    market_stop = asyncio.Event()
    ingest_task = asyncio.create_task(asyncio.sleep(0))
    state = MarketRecordState(
        market_id="m1",
        sink=sink,
        ingest_task=ingest_task,
        market_stop=market_stop,
    )
    session.markets["m1"] = state
    session.last_global_event_ts = sink.manifest.segments[0].last_recv_ts
    session.last_global_write_mono = time.monotonic()
    payload = build_heartbeat_payload(session, stall_s=120.0)
    assert payload["event_count"] == 1
    assert payload["active_market_count"] == 1
    assert payload["stall_detected"] is False
    ingest_task.cancel()


@pytest.mark.asyncio
async def test_heartbeat_stall_detected_when_no_recent_writes(tmp_path) -> None:
    import asyncio

    sink = EventSink(tmp_path / "rec", "m1")
    session = RecordSessionState(recording_started_ts="2026-07-03T00:00:00+00:00")
    session.last_global_write_mono = time.monotonic() - 200.0
    ingest_task = asyncio.create_task(asyncio.sleep(0))
    session.markets["m1"] = MarketRecordState(
        market_id="m1",
        sink=sink,
        ingest_task=ingest_task,
        market_stop=asyncio.Event(),
    )
    payload = build_heartbeat_payload(session, stall_s=120.0)
    assert payload["stall_detected"] is True
    ingest_task.cancel()


def test_coverage_report_marks_missing_market(tmp_path) -> None:
    session = RecordSessionState(recording_started_ts="2026-07-03T00:00:00+00:00")
    session.expected_market_ids = {"btc_5m_a", "btc_5m_b"}
    report = build_coverage_report(
        session,
        day_dir=tmp_path,
        recording_ended_ts="2026-07-03T01:00:00+00:00",
    )
    assert report["expected_market_count"] == 2
    assert report["recorded_market_count"] == 0
    assert report["coverage_pct"] == 0.0
    statuses = {m["market_id"]: m["status"] for m in report["markets"]}
    assert statuses["btc_5m_a"] == "missing"
    assert statuses["btc_5m_b"] == "missing"


@pytest.mark.asyncio
async def test_coverage_report_from_manifests_after_session_clear(tmp_path) -> None:
    day_dir = tmp_path / "2026-07-03"
    market_dir = day_dir / "btc_5m_20260703_1825"
    market_dir.mkdir(parents=True)
    sink = EventSink(market_dir, "btc_5m_20260703_1825", batch_size=1, batch_flush_ms=1)
    await sink.start()
    sink.emit(_event())
    await sink.stop()

    session = RecordSessionState(recording_started_ts="2026-07-03T18:22:13+00:00")
    session.expected_market_ids = {"btc_5m_20260703_1825", "btc_5m_20260703_1830"}
    session.markets.clear()

    report = build_coverage_report(
        session,
        day_dir=day_dir,
        recording_ended_ts="2026-07-03T18:23:16+00:00",
    )
    by_id = {m["market_id"]: m for m in report["markets"]}
    assert by_id["btc_5m_20260703_1825"]["status"] == "recorded"
    assert by_id["btc_5m_20260703_1825"]["event_count"] == 1
    assert by_id["btc_5m_20260703_1830"]["status"] == "missing"
    assert report["recorded_market_count"] == 1
    assert report["coverage_pct"] == 50.0


@pytest.mark.asyncio
async def test_final_session_artifacts_after_shutdown(tmp_path) -> None:
    day_dir = tmp_path / "2026-07-03"
    market_dir = day_dir / "btc_5m_20260703_1825"
    market_dir.mkdir(parents=True)
    sink = EventSink(market_dir, "btc_5m_20260703_1825", batch_size=1, batch_flush_ms=1)
    await sink.start()
    sink.emit(_event())
    await sink.stop()

    session = RecordSessionState(recording_started_ts="2026-07-03T18:22:13+00:00")
    session.expected_market_ids = {"btc_5m_20260703_1825"}
    session.markets.clear()

    _write_final_session_artifacts(
        day_dir=day_dir,
        session=session,
        stall_s=120.0,
        recording_ended_ts="2026-07-03T18:23:16+00:00",
    )

    coverage = json.loads((day_dir / "coverage_report.json").read_text(encoding="utf-8"))
    heartbeat = json.loads((day_dir / "heartbeat.json").read_text(encoding="utf-8"))
    manifest = json.loads((market_dir / "manifest.json").read_text(encoding="utf-8"))

    assert coverage["recorded_market_count"] == 1
    assert coverage["coverage_pct"] == 100.0
    assert heartbeat["event_count"] == 1
    assert heartbeat["active_market_count"] == 0
    assert manifest["recording_ended_ts"] is not None


@pytest.mark.asyncio
async def test_heartbeat_from_manifests_after_session_clear(tmp_path) -> None:
    day_dir = tmp_path / "2026-07-03"
    market_dir = day_dir / "btc_5m_20260703_1825"
    market_dir.mkdir(parents=True)
    sink = EventSink(market_dir, "btc_5m_20260703_1825", batch_size=1, batch_flush_ms=1)
    await sink.start()
    sink.emit(_event())
    await sink.stop()

    session = RecordSessionState(recording_started_ts="2026-07-03T18:22:13+00:00")
    payload = build_heartbeat_payload_from_manifests(
        session,
        day_dir=day_dir,
        stall_s=120.0,
        active_market_count=0,
    )
    assert payload["event_count"] == 1
    assert payload["dropped_events"] == 0
    assert payload["active_market_count"] == 0


@pytest.mark.asyncio
async def test_discovery_shutdown_writes_coverage_from_manifests(tmp_path, monkeypatch) -> None:
    import asyncio
    import json
    from pathlib import Path

    from tyrex_pm.ingestion.market_discovery import MarketDiscoveryResult
    from tyrex_pm.runtime.config import load_app_config
    from tyrex_pm.runtime.record_run import _run_discovery_record

    root = Path(__file__).resolve().parents[1]
    app = load_app_config(
        repo_root=root,
        strategy_file="config/strategies/paired_binary.yaml",
        scenario_file="config/scenarios/record_btc5m.yaml",
    )
    rec = app.runtime.recording
    day_dir = tmp_path / "2026-07-03"

    discovery_result = MarketDiscoveryResult(
        market_id="btc_5m_20260703_1825",
        condition_id="0xabc",
        yes_token_id="111",
        no_token_id="222",
        event_start_ts=1_700_000_000.0,
        event_end_ts=1_700_000_300.0,
        event_slug="btc-updown-5m-test",
    )

    async def _fake_ws(*_args, **kwargs) -> None:
        stop = kwargs.get("stop")
        if stop is not None:
            await stop.wait()

    async def _fake_discovery(*, stop, on_market_discovered, **_kwargs) -> None:
        await on_market_discovered(discovery_result)
        await stop.wait()

    monkeypatch.setattr("tyrex_pm.ingestion.market_ws_ingest.run_market_ws_ingest", _fake_ws)
    monkeypatch.setattr("tyrex_pm.runtime.record_run.run_market_discovery", _fake_discovery)
    monkeypatch.setattr("tyrex_pm.runtime.record_run._recording_day_dir", lambda _repo, _rec: day_dir)
    monkeypatch.setattr("tyrex_pm.runtime.record_run._git_sha", lambda _repo: "testsha")

    stop = asyncio.Event()

    async def _stop_soon() -> None:
        await asyncio.sleep(0.3)
        stop.set()

    asyncio.create_task(_stop_soon())
    await _run_discovery_record(
        app=app,
        rec=rec,
        repo_root=root,
        scenario="config/scenarios/record_btc5m.yaml",
        stop=stop,
    )

    coverage_path = day_dir / "coverage_report.json"
    heartbeat_path = day_dir / "heartbeat.json"
    manifest_path = day_dir / discovery_result.market_id / "manifest.json"
    assert coverage_path.exists()
    assert heartbeat_path.exists()
    assert manifest_path.exists()

    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id = {m["market_id"]: m for m in coverage["markets"]}

    assert manifest["recording_ended_ts"] is not None
    assert by_id[discovery_result.market_id]["status"] == "recorded"
    assert coverage["recorded_market_count"] == 1
    assert coverage["coverage_pct"] == 100.0


@pytest.mark.asyncio
async def test_heartbeat_loop_writes_file(tmp_path) -> None:
    import asyncio

    from tyrex_pm.runtime.record_run import _heartbeat_loop

    session = RecordSessionState(recording_started_ts="2026-07-03T00:00:00+00:00")
    stop = asyncio.Event()

    async def _stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    asyncio.create_task(_stop_soon())
    await _heartbeat_loop(
        day_dir=tmp_path,
        session=session,
        stop=stop,
        interval_s=0.01,
        stall_s=120.0,
    )
    heartbeat = tmp_path / "heartbeat.json"
    assert heartbeat.exists()
