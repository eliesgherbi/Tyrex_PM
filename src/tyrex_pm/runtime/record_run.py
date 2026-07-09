"""Record-only runtime — persist MarketEvents without trading (Phase 2B M2B.1-A/B)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tyrex_pm.core.errors import ConfigError
from tyrex_pm.core.events import EventType, MarketEvent, compute_event_id, compute_payload_digest
from tyrex_pm.ingestion.market_discovery import MarketDiscoveryResult, run_market_discovery
from tyrex_pm.reporting.event_sink import EventSink, _iso8601, _utc_now
from tyrex_pm.runtime.config import AppConfig, load_app_config
from tyrex_pm.state.market_store import MarketStateStore
from tyrex_pm.venue.binance_data.normalize import EXTERNAL_BTC_MARKET_ID
from tyrex_pm.venue.polymarket_rtds.normalize import REFERENCE_PRICES_MARKET_ID

log = logging.getLogger(__name__)

COVERAGE_REPORT_SCHEMA_VERSION = 1
HEARTBEAT_SCHEMA_VERSION = 1


@dataclass
class MarketRecordState:
    market_id: str
    sink: EventSink
    ingest_task: asyncio.Task[None]
    market_stop: asyncio.Event
    event_end_ts: float | None = None
    discovered_ts: str | None = None


@dataclass
class RecordSessionState:
    recording_started_ts: str
    expected_market_ids: set[str] = field(default_factory=set)
    markets: dict[str, MarketRecordState] = field(default_factory=dict)
    skipped_markets: dict[str, str] = field(default_factory=dict)
    external_btc_sink: EventSink | None = None
    external_btc_task: asyncio.Task[None] | None = None
    external_btc_stop: asyncio.Event | None = None
    reference_prices_sink: EventSink | None = None
    reference_prices_task: asyncio.Task[None] | None = None
    reference_prices_stop: asyncio.Event | None = None
    price_to_beat_tracker: Any | None = None
    stall_alerted: bool = False
    last_global_event_ts: str | None = None
    last_global_write_mono: float | None = None


def _repo_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    return Path(__file__).resolve().parents[3]


def _git_sha(repo_root: Path) -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_root,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


def _scenario_name(scenario_file: str) -> str:
    return Path(scenario_file).name


def _recording_day_dir(repo_root: Path, rec) -> Path:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base = Path(rec.output_dir)
    if not base.is_absolute():
        base = repo_root / base
    return base / day


def _recording_output_dir(day_dir: Path, *, market_id: str) -> Path:
    return day_dir / market_id


def _external_btc_output_dir(day_dir: Path) -> Path:
    return day_dir / "external" / "btc_binance"


def _reference_prices_output_dir(day_dir: Path) -> Path:
    return day_dir / "external" / "polymarket_rtds_chainlink"


def _resolve_record_tokens(
    app: AppConfig,
    rec,
    *,
    event_meta=None,
) -> tuple[str, str, str]:
    if event_meta is not None:
        return event_meta.market_id, str(event_meta.yes_token_id), str(event_meta.no_token_id)

    market_id = rec.market_id
    yes = rec.yes_token_id
    no = rec.no_token_id

    md_tokens = list(app.runtime.market_data.token_ids)
    if yes is None and len(md_tokens) >= 1:
        yes = md_tokens[0]
    if no is None and len(md_tokens) >= 2:
        no = md_tokens[1]
    if app.paired_binary is not None:
        if yes is None:
            yes = app.paired_binary.yes_token_id
        if no is None:
            no = app.paired_binary.no_token_id
        if market_id is None:
            market_id = app.paired_binary.market_id

    if not market_id:
        raise ConfigError("recording requires market_id (runtime.recording.market_id or paired_binary.market_id)")
    if not yes or not no:
        raise ConfigError(
            "recording requires yes_token_id and no_token_id "
            "(set runtime.recording or pass --event-url)"
        )
    return market_id, str(yes), str(no)


def _validate_record_token_source(rec, event_url: str | None, *, discovery_enabled: bool) -> None:
    if discovery_enabled:
        return
    if event_url is None and (rec.yes_token_id is None or rec.no_token_id is None):
        raise ConfigError(
            "record mode requires --event-url to resolve yes/no token ids "
            "(or set runtime.recording.yes_token_id and no_token_id explicitly, "
            "or enable runtime.recording.discovery_enabled)"
        )


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build_heartbeat_payload(session: RecordSessionState, *, stall_s: float) -> dict[str, Any]:
    now_mono = time.monotonic()
    active = len(session.markets)
    total_events = sum(m.sink.manifest.event_count for m in session.markets.values())
    total_dropped = sum(m.sink.dropped_events for m in session.markets.values())
    external_btc_events = 0
    external_btc_dropped = 0
    reference_price_events = 0
    reference_price_dropped = 0
    if session.external_btc_sink is not None:
        external_btc_events = session.external_btc_sink.manifest.event_count
        external_btc_dropped = session.external_btc_sink.dropped_events
        total_events += external_btc_events
        total_dropped += external_btc_dropped
    if session.reference_prices_sink is not None:
        reference_price_events = session.reference_prices_sink.manifest.event_count
        reference_price_dropped = session.reference_prices_sink.dropped_events
        total_events += reference_price_events
        total_dropped += reference_price_dropped
    last_write_mono = session.last_global_write_mono
    stalled = False
    if last_write_mono is not None and active > 0:
        stalled = (now_mono - last_write_mono) >= stall_s
    return {
        "schema_version": HEARTBEAT_SCHEMA_VERSION,
        "recording_started_ts": session.recording_started_ts,
        "updated_ts": _iso8601(_utc_now()),
        "last_event_ts": session.last_global_event_ts,
        "last_write_ts": _iso8601(_utc_now()) if last_write_mono is not None else None,
        "event_count": total_events,
        "dropped_events": total_dropped,
        "active_market_count": active,
        "external_btc_event_count": external_btc_events,
        "external_btc_dropped_events": external_btc_dropped,
        "reference_price_event_count": reference_price_events,
        "reference_price_dropped_events": reference_price_dropped,
        "stall_detected": stalled,
        "stall_threshold_s": stall_s,
    }


async def _heartbeat_loop(
    *,
    day_dir: Path,
    session: RecordSessionState,
    stop: asyncio.Event,
    interval_s: float,
    stall_s: float,
) -> None:
    heartbeat_path = day_dir / "heartbeat.json"
    while not stop.is_set():
        payload = build_heartbeat_payload(session, stall_s=stall_s)
        _write_json_atomic(heartbeat_path, payload)
        if payload["stall_detected"] and not session.stall_alerted:
            session.stall_alerted = True
            log.warning(
                "record heartbeat stall detected: no events for >=%ss (active_markets=%s)",
                stall_s,
                payload["active_market_count"],
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(1.0, interval_s))
        except asyncio.TimeoutError:
            continue
    _write_json_atomic(heartbeat_path, build_heartbeat_payload(session, stall_s=stall_s))


def _load_manifest_summaries_from_day_dir(
    day_dir: Path,
    *,
    session_started_ts: str | None = None,
) -> dict[str, dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    if not day_dir.is_dir():
        return summaries
    for manifest_path in sorted(day_dir.glob("*/manifest.json")):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("coverage_report: skip unreadable manifest %s: %r", manifest_path, exc)
            continue
        started = data.get("recording_started_ts")
        if session_started_ts and started and started < session_started_ts:
            continue
        market_id = str(data.get("market_id") or manifest_path.parent.name)
        summaries[market_id] = data
    return summaries


def _market_summary_from_manifest(data: dict[str, Any]) -> dict[str, Any]:
    segments = data.get("segments") or []
    first_segment = segments[0] if segments else None
    last_segment = segments[-1] if segments else None
    event_count = sum(int(seg.get("event_count", 0)) for seg in segments)
    status = "recorded" if event_count > 0 else "partial"
    return {
        "market_id": data.get("market_id"),
        "event_count": event_count,
        "first_recv_ts": first_segment.get("first_recv_ts") if first_segment else None,
        "last_recv_ts": last_segment.get("last_recv_ts") if last_segment else None,
        "dropped_events": int(data.get("dropped_events", 0)),
        "gap_count": len(data.get("gaps") or []),
        "status": status,
        "recording_ended_ts": data.get("recording_ended_ts"),
    }


def build_coverage_report(
    session: RecordSessionState,
    *,
    day_dir: Path | None = None,
    recording_ended_ts: str | None,
) -> dict[str, Any]:
    manifest_summaries = (
        _load_manifest_summaries_from_day_dir(
            day_dir,
            session_started_ts=session.recording_started_ts,
        )
        if day_dir is not None
        else {}
    )
    all_market_ids = set(session.expected_market_ids)
    all_market_ids.update(manifest_summaries.keys())
    all_market_ids.update(session.skipped_markets.keys())

    markets_out: list[dict[str, Any]] = []
    for market_id in sorted(all_market_ids):
        if market_id in session.skipped_markets:
            markets_out.append(
                {
                    "market_id": market_id,
                    "event_count": 0,
                    "first_recv_ts": None,
                    "last_recv_ts": None,
                    "dropped_events": 0,
                    "gap_count": 0,
                    "status": "skipped",
                    "skip_reason": session.skipped_markets[market_id],
                }
            )
            continue
        manifest_data = manifest_summaries.get(market_id)
        if manifest_data is not None:
            markets_out.append(_market_summary_from_manifest(manifest_data))
            continue
        state = session.markets.get(market_id)
        if state is None:
            markets_out.append(
                {
                    "market_id": market_id,
                    "event_count": 0,
                    "first_recv_ts": None,
                    "last_recv_ts": None,
                    "dropped_events": 0,
                    "gap_count": 0,
                    "status": "missing",
                }
            )
            continue
        manifest = state.sink.manifest
        seg = manifest.segments[-1] if manifest.segments else None
        status = "recorded" if manifest.event_count > 0 else "partial"
        markets_out.append(
            {
                "market_id": market_id,
                "event_count": manifest.event_count,
                "first_recv_ts": seg.first_recv_ts if seg else None,
                "last_recv_ts": seg.last_recv_ts if seg else None,
                "dropped_events": manifest.dropped_events,
                "gap_count": manifest.gap_count,
                "status": status,
            }
        )

    expected = len(session.expected_market_ids) or len(
        [m for m in markets_out if m["status"] not in {"skipped"}]
    )
    recorded = sum(1 for m in markets_out if m["status"] == "recorded")
    coverage_pct = (100.0 * recorded / expected) if expected else 0.0
    return {
        "schema_version": COVERAGE_REPORT_SCHEMA_VERSION,
        "recording_started_ts": session.recording_started_ts,
        "recording_ended_ts": recording_ended_ts,
        "expected_market_count": expected,
        "recorded_market_count": recorded,
        "coverage_pct": round(coverage_pct, 2),
        "markets": markets_out,
    }


def build_heartbeat_payload_from_manifests(
    session: RecordSessionState,
    *,
    day_dir: Path,
    stall_s: float,
    active_market_count: int = 0,
) -> dict[str, Any]:
    summaries = _load_manifest_summaries_from_day_dir(
        day_dir,
        session_started_ts=session.recording_started_ts,
    )
    total_events = 0
    total_dropped = 0
    last_event_ts: str | None = None
    for data in summaries.values():
        segments = data.get("segments") or []
        total_events += sum(int(seg.get("event_count", 0)) for seg in segments)
        total_dropped += int(data.get("dropped_events", 0))
        for seg in segments:
            recv = seg.get("last_recv_ts")
            if recv and (last_event_ts is None or recv > last_event_ts):
                last_event_ts = recv
    ext_manifest_path = day_dir / "external" / "btc_binance" / "manifest.json"
    if ext_manifest_path.is_file():
        try:
            ext_data = json.loads(ext_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            ext_data = None
        if ext_data is not None:
            segments = ext_data.get("segments") or []
            total_events += sum(int(seg.get("event_count", 0)) for seg in segments)
            total_dropped += int(ext_data.get("dropped_events", 0))
            for seg in segments:
                recv = seg.get("last_recv_ts")
                if recv and (last_event_ts is None or recv > last_event_ts):
                    last_event_ts = recv
    if last_event_ts is None:
        last_event_ts = session.last_global_event_ts
    return {
        "schema_version": HEARTBEAT_SCHEMA_VERSION,
        "recording_started_ts": session.recording_started_ts,
        "updated_ts": _iso8601(_utc_now()),
        "last_event_ts": last_event_ts,
        "last_write_ts": _iso8601(_utc_now()) if last_event_ts is not None else None,
        "event_count": total_events,
        "dropped_events": total_dropped,
        "active_market_count": active_market_count,
        "stall_detected": False,
        "stall_threshold_s": stall_s,
    }


def _write_final_session_artifacts(
    *,
    day_dir: Path,
    session: RecordSessionState,
    stall_s: float,
    recording_ended_ts: str | None,
) -> None:
    _write_json_atomic(
        day_dir / "heartbeat.json",
        build_heartbeat_payload_from_manifests(
            session,
            day_dir=day_dir,
            stall_s=stall_s,
            active_market_count=0,
        ),
    )
    _write_json_atomic(
        day_dir / "coverage_report.json",
        build_coverage_report(session, day_dir=day_dir, recording_ended_ts=recording_ended_ts),
    )


def _make_market_discovered_event(result: MarketDiscoveryResult) -> MarketEvent:
    recv = _utc_now()
    payload = {
        "event_slug": result.event_slug,
        "condition_id": result.condition_id,
        "yes_token_id": result.yes_token_id,
        "no_token_id": result.no_token_id,
        "event_start_ts": result.event_start_ts,
        "event_end_ts": result.event_end_ts,
    }
    digest = compute_payload_digest(payload)
    return MarketEvent(
        event_id=compute_event_id(
            event_type=EventType.MARKET_DISCOVERED.value,
            token_id=None,
            venue_cursor=result.event_slug,
            source_ts=recv,
            payload_digest=digest,
        ),
        event_type=EventType.MARKET_DISCOVERED,
        market_id=result.market_id,
        token_id=None,
        venue_cursor=result.event_slug,
        source_ts=recv,
        recv_ts=recv,
        payload=payload,
    )


def _register_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    def _request_stop(*_args) -> None:
        stop.set()

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            pass


def _event_written_hook(session: RecordSessionState):
    def _on_event_written(event: MarketEvent) -> None:
        session.last_global_event_ts = _iso8601(event.recv_ts)
        session.last_global_write_mono = time.monotonic()

    return _on_event_written


def _make_event_sink(
    output_dir: Path,
    market_id: str,
    *,
    yes_token_id: str,
    no_token_id: str,
    rec,
    git_sha: str,
    scenario: str,
    session: RecordSessionState,
) -> EventSink:
    return EventSink(
        output_dir,
        market_id,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        queue_maxsize=rec.queue_maxsize,
        segment_max_mb=rec.segment_max_mb,
        segment_max_s=rec.segment_max_s,
        batch_size=rec.batch_size,
        batch_flush_ms=rec.batch_flush_ms,
        git_sha=git_sha,
        scenario=scenario,
        compress=rec.compress,
        on_event_written=_event_written_hook(session),
    )


async def _stop_market_recording(state: MarketRecordState) -> None:
    state.market_stop.set()
    try:
        await asyncio.wait_for(state.ingest_task, timeout=15.0)
    except asyncio.TimeoutError:
        state.ingest_task.cancel()
        try:
            await state.ingest_task
        except asyncio.CancelledError:
            pass
    await state.sink.stop()


async def _market_lifecycle_loop(
    state: MarketRecordState,
    *,
    post_close_grace_s: float,
    session: RecordSessionState,
    auto_cleanup: bool,
    global_stop: asyncio.Event | None = None,
) -> None:
    if state.event_end_ts is None:
        return
    deadline = state.event_end_ts + post_close_grace_s
    while not state.market_stop.is_set():
        remaining = deadline - time.time()
        if remaining <= 0:
            log.info("market recording window complete market_id=%s", state.market_id)
            state.market_stop.set()
            if global_stop is not None:
                global_stop.set()
            break
        try:
            await asyncio.wait_for(state.market_stop.wait(), timeout=min(remaining, 5.0))
        except asyncio.TimeoutError:
            continue
    if auto_cleanup and state.market_id in session.markets:
        await _stop_market_recording(state)
        session.markets.pop(state.market_id, None)


async def _start_market_recording(
    *,
    result: MarketDiscoveryResult,
    app: AppConfig,
    rec,
    day_dir: Path,
    session: RecordSessionState,
    git_sha: str,
    scenario: str,
) -> None:
    if result.market_id in session.markets:
        return
    session.expected_market_ids.add(result.market_id)
    output_dir = _recording_output_dir(day_dir, market_id=result.market_id)
    sink = _make_event_sink(
        output_dir,
        result.market_id,
        yes_token_id=result.yes_token_id,
        no_token_id=result.no_token_id,
        rec=rec,
        git_sha=git_sha,
        scenario=scenario,
        session=session,
    )
    await sink.start()
    sink.emit(_make_market_discovered_event(result))

    md = app.runtime.market_data
    eb = md.event_backbone
    ws = md.websocket
    coord = SimpleNamespace(market_state=MarketStateStore(store_top_n_levels=md.store_top_n_levels))
    market_stop = asyncio.Event()

    from tyrex_pm.ingestion.market_ws_ingest import run_market_ws_ingest

    ingest_task = asyncio.create_task(
        run_market_ws_ingest(
            coord,
            [result.yes_token_id, result.no_token_id],
            stop=market_stop,
            shadow_store=None,
            authoritative_store=coord.market_state,
            primary_mode=True,
            url=ws.url,
            reconnect_backoff_s=ws.reconnect_backoff_s,
            event_backbone_config_flag=True,
            event_backbone_market_id=result.market_id,
            event_backbone_reorder_buffer_ms=eb.reorder_buffer_ms,
            on_market_event_emitted=sink.emit,
        )
    )
    state = MarketRecordState(
        market_id=result.market_id,
        sink=sink,
        ingest_task=ingest_task,
        market_stop=market_stop,
        event_end_ts=result.event_end_ts,
        discovered_ts=_iso8601(_utc_now()),
    )
    session.markets[result.market_id] = state
    if session.price_to_beat_tracker is not None and app.runtime.reference_prices.emit_price_to_beat:
        session.price_to_beat_tracker.register_market(
            market_id=result.market_id,
            event_start_ts=result.event_start_ts,
            event_end_ts=result.event_end_ts,
        )
    asyncio.create_task(
        _market_lifecycle_loop(
            state,
            post_close_grace_s=rec.post_close_grace_s,
            session=session,
            auto_cleanup=True,
        )
    )
    log.info("recording market events market_id=%s dir=%s", result.market_id, output_dir)


async def _start_external_btc_recording(
    *,
    app: AppConfig,
    rec,
    day_dir: Path,
    session: RecordSessionState,
    git_sha: str,
    scenario: str,
) -> None:
    ext = app.runtime.external_btc
    if not ext.enabled or session.external_btc_sink is not None:
        return
    output_dir = _external_btc_output_dir(day_dir)
    sink = _make_event_sink(
        output_dir,
        EXTERNAL_BTC_MARKET_ID,
        yes_token_id="external_btc",
        no_token_id="external_btc",
        rec=rec,
        git_sha=git_sha,
        scenario=scenario,
        session=session,
    )
    await sink.start()
    session.external_btc_sink = sink
    ext_stop = asyncio.Event()
    session.external_btc_stop = ext_stop

    from tyrex_pm.ingestion.external_btc import run_external_btc_ingest

    session.external_btc_task = asyncio.create_task(
        run_external_btc_ingest(
            symbol=ext.symbol,
            streams=ext.streams,
            venue=ext.venue,
            stop=ext_stop,
            on_event_emitted=sink.emit,
            reconnect_backoff_s=ext.reconnect_backoff_s,
            clock_sync_interval_s=ext.clock_sync_interval_s,
        )
    )
    log.info("recording external BTC feed dir=%s symbol=%s streams=%s", output_dir, ext.symbol, ext.streams)


async def _start_reference_prices_recording(
    *,
    app: AppConfig,
    rec,
    day_dir: Path,
    session: RecordSessionState,
    git_sha: str,
    scenario: str,
) -> None:
    ref_cfg = app.runtime.reference_prices
    if not ref_cfg.enabled or session.reference_prices_sink is not None:
        return
    output_dir = _reference_prices_output_dir(day_dir)
    sink = _make_event_sink(
        output_dir,
        REFERENCE_PRICES_MARKET_ID,
        yes_token_id="reference_price",
        no_token_id="reference_price",
        rec=rec,
        git_sha=git_sha,
        scenario=scenario,
        session=session,
    )
    await sink.start()
    session.reference_prices_sink = sink
    ref_stop = asyncio.Event()
    session.reference_prices_stop = ref_stop

    tracker = None
    if ref_cfg.emit_price_to_beat:
        from tyrex_pm.ingestion.price_to_beat_tracker import PriceToBeatTracker

        tracker = PriceToBeatTracker(
            max_lag_ms=ref_cfg.price_to_beat_max_lag_ms,
            source_label="polymarket_rtds_chainlink",
        )
        session.price_to_beat_tracker = tracker

    from tyrex_pm.ingestion.reference_prices import run_reference_prices_ingest

    def _on_reference_event(event: MarketEvent) -> None:
        if event.event_type == EventType.PRICE_TO_BEAT_OBSERVED:
            mid = event.market_id
            if mid and mid in session.markets:
                session.markets[mid].sink.emit(event)
            return
        sink.emit(event)

    session.reference_prices_task = asyncio.create_task(
        run_reference_prices_ingest(
            feeds=ref_cfg.feeds,
            symbols=ref_cfg.symbols,
            venue=ref_cfg.venue,
            stop=ref_stop,
            on_event_emitted=_on_reference_event,
            price_to_beat_tracker=tracker,
            reconnect_backoff_s=ref_cfg.reconnect_backoff_s,
        )
    )
    log.info(
        "recording reference prices dir=%s feeds=%s symbols=%s",
        output_dir,
        ref_cfg.feeds,
        ref_cfg.symbols,
    )


async def _stop_reference_prices_recording(session: RecordSessionState) -> None:
    if session.reference_prices_stop is not None:
        session.reference_prices_stop.set()
    if session.reference_prices_task is not None:
        try:
            await asyncio.wait_for(session.reference_prices_task, timeout=15.0)
        except asyncio.TimeoutError:
            session.reference_prices_task.cancel()
            try:
                await session.reference_prices_task
            except asyncio.CancelledError:
                pass
    if session.reference_prices_sink is not None:
        await session.reference_prices_sink.stop()
    session.reference_prices_sink = None
    session.reference_prices_task = None
    session.reference_prices_stop = None
    session.price_to_beat_tracker = None


async def _stop_external_btc_recording(session: RecordSessionState) -> None:
    if session.external_btc_stop is not None:
        session.external_btc_stop.set()
    if session.external_btc_task is not None:
        try:
            await asyncio.wait_for(session.external_btc_task, timeout=15.0)
        except asyncio.TimeoutError:
            session.external_btc_task.cancel()
            try:
                await session.external_btc_task
            except asyncio.CancelledError:
                pass
    if session.external_btc_sink is not None:
        await session.external_btc_sink.stop()
    session.external_btc_sink = None
    session.external_btc_task = None
    session.external_btc_stop = None


async def _run_single_market_record(
    *,
    app: AppConfig,
    rec,
    repo_root: Path,
    scenario: str,
    event_meta,
    stop: asyncio.Event,
) -> RecordSessionState:
    market_id, yes_token_id, no_token_id = _resolve_record_tokens(app, rec, event_meta=event_meta)
    day_dir = _recording_day_dir(repo_root, rec)
    output_dir = _recording_output_dir(day_dir, market_id=market_id)
    session = RecordSessionState(recording_started_ts=_iso8601(_utc_now()) or "")
    session.expected_market_ids.add(market_id)

    sink = _make_event_sink(
        output_dir,
        market_id,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        rec=rec,
        git_sha=_git_sha(repo_root),
        scenario=_scenario_name(scenario),
        session=session,
    )
    await sink.start()

    md = app.runtime.market_data
    eb = md.event_backbone
    ws = md.websocket
    coord = SimpleNamespace(market_state=MarketStateStore(store_top_n_levels=md.store_top_n_levels))
    market_stop = asyncio.Event()

    from tyrex_pm.ingestion.market_ws_ingest import run_market_ws_ingest

    ingest_task = asyncio.create_task(
        run_market_ws_ingest(
            coord,
            [yes_token_id, no_token_id],
            stop=market_stop,
            shadow_store=None,
            authoritative_store=coord.market_state,
            primary_mode=True,
            url=ws.url,
            reconnect_backoff_s=ws.reconnect_backoff_s,
            event_backbone_config_flag=True,
            event_backbone_market_id=market_id,
            event_backbone_reorder_buffer_ms=eb.reorder_buffer_ms,
            on_market_event_emitted=sink.emit,
        )
    )
    state = MarketRecordState(
        market_id=market_id,
        sink=sink,
        ingest_task=ingest_task,
        market_stop=market_stop,
        event_end_ts=getattr(event_meta, "event_end_ts", None) if event_meta is not None else None,
    )
    session.markets[market_id] = state

    async def _bridge_stop() -> None:
        await stop.wait()
        market_stop.set()

    asyncio.create_task(_bridge_stop())
    if state.event_end_ts is not None:
        asyncio.create_task(
            _market_lifecycle_loop(
                state,
                post_close_grace_s=rec.post_close_grace_s,
                session=session,
                auto_cleanup=False,
                global_stop=stop,
            )
        )

    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(
            day_dir=day_dir,
            session=session,
            stop=stop,
            interval_s=rec.heartbeat_interval_s,
            stall_s=rec.heartbeat_stall_s,
        )
    )
    await _start_external_btc_recording(
        app=app,
        rec=rec,
        day_dir=day_dir,
        session=session,
        git_sha=_git_sha(repo_root),
        scenario=_scenario_name(scenario),
    )
    await _start_reference_prices_recording(
        app=app,
        rec=rec,
        day_dir=day_dir,
        session=session,
        git_sha=_git_sha(repo_root),
        scenario=_scenario_name(scenario),
    )
    if session.price_to_beat_tracker is not None and event_meta is not None:
        session.price_to_beat_tracker.register_market(
            market_id=market_id,
            event_start_ts=float(event_meta.event_start_ts),
            event_end_ts=float(event_meta.event_end_ts),
        )

    log.info("recording market events market_id=%s dir=%s (WS only)", market_id, output_dir)
    try:
        try:
            await stop.wait()
        except asyncio.CancelledError:
            log.info("record stop requested (cancelled)")
    finally:
        stop.set()
        market_stop.set()
        await _stop_market_recording(state)
        await _stop_external_btc_recording(session)
        await _stop_reference_prices_recording(session)
        session.markets.pop(market_id, None)
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        ended = _iso8601(_utc_now())
        _write_final_session_artifacts(
            day_dir=day_dir,
            session=session,
            stall_s=rec.heartbeat_stall_s,
            recording_ended_ts=ended,
        )
        log.info(
            "recording stopped events=%s dropped=%s manifest=%s",
            sink.manifest.event_count,
            sink.dropped_events,
            output_dir / "manifest.json",
        )
    return session


async def _run_discovery_record(
    *,
    app: AppConfig,
    rec,
    repo_root: Path,
    scenario: str,
    stop: asyncio.Event,
) -> RecordSessionState:
    day_dir = _recording_day_dir(repo_root, rec)
    session = RecordSessionState(recording_started_ts=_iso8601(_utc_now()) or "")
    git_sha = _git_sha(repo_root)
    scenario_name = _scenario_name(scenario)

    async def _on_discovered(result: MarketDiscoveryResult) -> None:
        await _start_market_recording(
            result=result,
            app=app,
            rec=rec,
            day_dir=day_dir,
            session=session,
            git_sha=git_sha,
            scenario=scenario_name,
        )

    async def _on_skipped(result: MarketDiscoveryResult, reason: str) -> None:
        session.skipped_markets[result.market_id] = reason

    discovery_task = asyncio.create_task(
        run_market_discovery(
            stop=stop,
            on_market_discovered=_on_discovered,
            on_market_skipped=_on_skipped,
            poll_interval_s=rec.discovery_poll_s,
            post_close_grace_s=rec.post_close_grace_s,
            pre_open_recording_lead_s=rec.pre_open_recording_lead_s,
            skip_expired_markets=rec.skip_expired_markets,
        )
    )
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(
            day_dir=day_dir,
            session=session,
            stop=stop,
            interval_s=rec.heartbeat_interval_s,
            stall_s=rec.heartbeat_stall_s,
        )
    )
    await _start_external_btc_recording(
        app=app,
        rec=rec,
        day_dir=day_dir,
        session=session,
        git_sha=git_sha,
        scenario=scenario_name,
    )
    await _start_reference_prices_recording(
        app=app,
        rec=rec,
        day_dir=day_dir,
        session=session,
        git_sha=git_sha,
        scenario=scenario_name,
    )

    log.info("recording discovery mode day_dir=%s poll_s=%s", day_dir, rec.discovery_poll_s)
    try:
        try:
            await stop.wait()
        except asyncio.CancelledError:
            log.info("record stop requested (cancelled)")
    finally:
        stop.set()
        discovery_task.cancel()
        try:
            await discovery_task
        except asyncio.CancelledError:
            pass
        for market_id in list(session.markets.keys()):
            await _stop_market_recording(session.markets[market_id])
        session.markets.clear()
        await _stop_external_btc_recording(session)
        await _stop_reference_prices_recording(session)
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        ended = _iso8601(_utc_now())
        _write_final_session_artifacts(
            day_dir=day_dir,
            session=session,
            stall_s=rec.heartbeat_stall_s,
            recording_ended_ts=ended,
        )
        log.info("discovery recording stopped markets=%s", len(session.expected_market_ids))
    return session


async def cmd_record(args: argparse.Namespace) -> int:
    repo_root = _repo_root(getattr(args, "repo_root", None))
    scenario = getattr(args, "scenario", None)
    if not scenario:
        raise ConfigError("--scenario is required for record mode")

    app = load_app_config(
        repo_root=repo_root,
        strategy_file="config/strategies/paired_binary.yaml",
        scenario_file=scenario,
    )
    event_url = getattr(args, "event_url", None)
    event_meta = None
    if event_url:
        if app.paired_binary is None:
            raise ConfigError("record --event-url requires paired_binary strategy config")
        from tyrex_pm.runtime.paired_binary_metadata import resolve_and_apply_paired_binary_metadata
        from tyrex_pm.venue.polymarket.event_metadata import resolve_paired_binary_event_metadata

        event_meta = resolve_paired_binary_event_metadata(event_url)
        app, _ = resolve_and_apply_paired_binary_metadata(app, event_url=event_url)

    rec = app.runtime.recording
    if not rec.enabled:
        raise ConfigError("runtime.recording.enabled must be true for tyrex-pm record")
    if not app.runtime.market_data.enabled:
        raise ConfigError("record mode requires runtime.market_data.enabled=true")
    if not app.runtime.market_data.event_backbone.enabled:
        raise ConfigError("record mode requires runtime.market_data.event_backbone.enabled=true")
    if rec.tap_in_live:
        raise ConfigError("runtime.recording.tap_in_live is not implemented in M2B.1-B; keep false")

    discovery_mode = rec.discovery_enabled and event_url is None
    _validate_record_token_source(rec, event_url, discovery_enabled=discovery_mode)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    _register_signal_handlers(loop, stop)

    if discovery_mode:
        await _run_discovery_record(app=app, rec=rec, repo_root=repo_root, scenario=scenario, stop=stop)
    else:
        await _run_single_market_record(
            app=app,
            rec=rec,
            repo_root=repo_root,
            scenario=scenario,
            event_meta=event_meta,
            stop=stop,
        )
    return 0
