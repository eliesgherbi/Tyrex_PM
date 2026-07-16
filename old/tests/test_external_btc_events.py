"""External BTC event normalization and record integration tests (M2B.2)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tyrex_pm.core.events import EventType, event_from_dict, event_to_dict
from tyrex_pm.venue.binance_data.normalize import (
    build_clock_sync_event,
    build_external_btc_tick_event,
    normalize_agg_trade,
    normalize_book_ticker,
)

BOOK_TICKER_RAW = {
    "u": 400900217,
    "s": "BTCUSDT",
    "b": "67234.12000000",
    "B": "2.40300000",
    "a": "67234.13000000",
    "A": "4.35000000",
}

AGG_TRADE_RAW = {
    "e": "aggTrade",
    "E": 1672515782136,
    "s": "BTCUSDT",
    "a": 12345,
    "p": "67234.12500000",
    "q": "0.00100000",
    "f": 100,
    "l": 105,
    "T": 1672515782130,
    "m": True,
    "M": True,
}


def _recv() -> datetime:
    return datetime(2026, 7, 3, 19, 0, 0, tzinfo=timezone.utc)


def test_book_ticker_normalization() -> None:
    payload = normalize_book_ticker(BOOK_TICKER_RAW, symbol="BTCUSDT")
    assert payload["source"] == "binance"
    assert payload["symbol"] == "BTCUSDT"
    assert payload["stream"] == "bookTicker"
    assert payload["bid"] == "67234.12000000"
    assert payload["ask"] == "67234.13000000"
    assert Decimal(payload["mid"]) == Decimal("67234.12500000")
    assert payload["raw"] == BOOK_TICKER_RAW


def test_agg_trade_normalization() -> None:
    payload = normalize_agg_trade(AGG_TRADE_RAW, symbol="BTCUSDT")
    assert payload["stream"] == "aggTrade"
    assert payload["price"] == "67234.12500000"
    assert payload["quantity"] == "0.00100000"
    assert payload["trade_id"] == "12345"
    expected = datetime.fromtimestamp(AGG_TRADE_RAW["T"] / 1000.0, tz=timezone.utc).isoformat()
    assert payload["source_event_ts"] == expected
    assert payload["raw"] == AGG_TRADE_RAW


def test_external_btc_tick_event_round_trip() -> None:
    payload = normalize_book_ticker(BOOK_TICKER_RAW, symbol="BTCUSDT")
    event = build_external_btc_tick_event(payload, recv_ts=_recv(), venue_cursor="400900217")
    assert event.event_type == EventType.EXTERNAL_BTC_TICK
    assert event.market_id == "external/btc_binance"
    assert event.token_id is None
    restored = event_from_dict(event_to_dict(event))
    assert restored.event_id == event.event_id
    assert restored.payload["raw"] == BOOK_TICKER_RAW


def test_external_btc_tick_event_id_deterministic() -> None:
    payload = normalize_agg_trade(AGG_TRADE_RAW, symbol="BTCUSDT")
    recv = _recv()
    source_ts = datetime.fromtimestamp(AGG_TRADE_RAW["T"] / 1000.0, tz=timezone.utc)
    a = build_external_btc_tick_event(payload, recv_ts=recv, source_ts=source_ts, venue_cursor="12345")
    b = build_external_btc_tick_event(payload, recv_ts=recv, source_ts=source_ts, venue_cursor="12345")
    assert a.event_id == b.event_id


def test_clock_sync_event_payload() -> None:
    recv = _recv()
    source = datetime(2026, 7, 3, 18, 59, 59, 500000, tzinfo=timezone.utc)
    event = build_clock_sync_event(symbol="BTCUSDT", venue="binance", recv_ts=recv, source_ts=source)
    assert event.event_type == EventType.CLOCK_SYNC
    assert event.payload["source"] == "binance"
    assert event.payload["latency_ms"] == 500.0
    round_trip = event_from_dict(event_to_dict(event))
    assert round_trip.event_type == EventType.CLOCK_SYNC


def test_clock_sync_null_source_ts() -> None:
    recv = _recv()
    event = build_clock_sync_event(symbol="BTCUSDT", venue="binance", recv_ts=recv, source_ts=None)
    assert event.payload["source_ts"] is None
    assert event.payload["latency_ms"] is None


@pytest.mark.asyncio
async def test_external_btc_ingest_emits_events_from_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    from tyrex_pm.ingestion.external_btc import run_external_btc_ingest

    seen: list[str] = []
    stop = asyncio.Event()

    class _FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def run(self, *, stop, on_message, reconnect_backoff_s=3.0) -> None:
            await on_message("btcusdt@bookTicker", BOOK_TICKER_RAW)
            await on_message("btcusdt@aggTrade", AGG_TRADE_RAW)
            stop.set()

    monkeypatch.setattr("tyrex_pm.ingestion.external_btc.BinanceDataWsClient", _FakeClient)

    def _on_event(event) -> None:
        seen.append(event.event_type.value)

    await run_external_btc_ingest(
        symbol="BTCUSDT",
        streams=("bookTicker", "aggTrade"),
        venue="binance",
        stop=stop,
        on_event_emitted=_on_event,
        clock_sync_interval_s=3600.0,
    )
    assert EventType.EXTERNAL_BTC_TICK.value in seen
    assert EventType.CLOCK_SYNC.value in seen
    assert seen.count(EventType.EXTERNAL_BTC_TICK.value) == 2


@pytest.mark.asyncio
async def test_record_run_starts_external_btc_when_enabled(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    from pathlib import Path

    from tyrex_pm.runtime.config import load_app_config
    from tyrex_pm.runtime.record_run import _run_discovery_record

    root = Path(__file__).resolve().parents[1]
    app = load_app_config(
        repo_root=root,
        strategy_file="config/strategies/paired_binary.yaml",
        scenario_file="config/scenarios/record_btc5m.yaml",
    )
    app = replace(
        app,
        runtime=replace(
            app.runtime,
            external_btc=replace(app.runtime.external_btc, enabled=True),
        ),
    )
    rec = app.runtime.recording
    day_dir = tmp_path / "2026-07-03"
    started: list[str] = []

    async def _fake_ingest(**kwargs) -> None:
        started.append("external")
        await kwargs["stop"].wait()

    async def _fake_discovery(*, stop, on_market_discovered, **_kwargs) -> None:
        await stop.wait()

    monkeypatch.setattr("tyrex_pm.ingestion.external_btc.run_external_btc_ingest", _fake_ingest)
    monkeypatch.setattr("tyrex_pm.runtime.record_run.run_market_discovery", _fake_discovery)
    monkeypatch.setattr("tyrex_pm.runtime.record_run._recording_day_dir", lambda _repo, _rec: day_dir)

    stop = asyncio.Event()

    async def _stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    asyncio.create_task(_stop_soon())
    await _run_discovery_record(
        app=app,
        rec=rec,
        repo_root=root,
        scenario="config/scenarios/record_btc5m.yaml",
        stop=stop,
    )
    assert started == ["external"]


@pytest.mark.asyncio
async def test_record_run_skips_external_btc_when_disabled(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

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
    started: list[str] = []

    async def _fake_ingest(**kwargs) -> None:
        started.append("external")

    async def _fake_discovery(*, stop, on_market_discovered, **_kwargs) -> None:
        await stop.wait()

    monkeypatch.setattr("tyrex_pm.ingestion.external_btc.run_external_btc_ingest", _fake_ingest)
    monkeypatch.setattr("tyrex_pm.runtime.record_run.run_market_discovery", _fake_discovery)
    monkeypatch.setattr("tyrex_pm.runtime.record_run._recording_day_dir", lambda _repo, _rec: day_dir)

    stop = asyncio.Event()

    async def _stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    asyncio.create_task(_stop_soon())
    await _run_discovery_record(
        app=app,
        rec=rec,
        repo_root=root,
        scenario="config/scenarios/record_btc5m.yaml",
        stop=stop,
    )
    assert started == []


@pytest.mark.asyncio
async def test_external_btc_sink_writes_separate_folder(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    from pathlib import Path

    from tyrex_pm.runtime.config import load_app_config
    from tyrex_pm.runtime.record_run import RecordSessionState, _iso8601, _start_external_btc_recording, _utc_now

    root = Path(__file__).resolve().parents[1]
    app = load_app_config(
        repo_root=root,
        strategy_file="config/strategies/paired_binary.yaml",
        scenario_file="config/scenarios/record_btc5m.yaml",
    )
    app = replace(
        app,
        runtime=replace(
            app.runtime,
            external_btc=replace(app.runtime.external_btc, enabled=True),
        ),
    )
    rec = app.runtime.recording
    day_dir = tmp_path / "2026-07-03"
    session = RecordSessionState(recording_started_ts=_iso8601(_utc_now()) or "")

    async def _fake_ingest(**kwargs) -> None:
        on_event = kwargs["on_event_emitted"]
        payload = normalize_book_ticker(BOOK_TICKER_RAW, symbol="BTCUSDT")
        on_event(build_external_btc_tick_event(payload, recv_ts=_recv(), venue_cursor="1"))
        kwargs["stop"].set()

    monkeypatch.setattr("tyrex_pm.ingestion.external_btc.run_external_btc_ingest", _fake_ingest)

    await _start_external_btc_recording(
        app=app,
        rec=rec,
        day_dir=day_dir,
        session=session,
        git_sha="testsha",
        scenario="record_btc5m.yaml",
    )
    if session.external_btc_task is not None:
        await session.external_btc_task
    if session.external_btc_sink is not None:
        await session.external_btc_sink.stop()

    manifest_path = day_dir / "external" / "btc_binance" / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["market_id"] == "external/btc_binance"
    assert manifest["event_types_seen"].get("external_btc_tick", 0) >= 1
