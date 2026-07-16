"""Bounded PTB capture tests."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tyrex_pm.ingestion.price_to_beat_tracker import (
    PTB_STATUS_MISSING,
    PTB_STATUS_OBSERVED,
    PTB_STATUS_OBSERVED_FROM_LOG,
)
from tyrex_pm.runtime.btc_5m_metadata import Btc5mMarketMetadata
from tyrex_pm.runtime.config import ZGapPtbConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.z_gap_boundary_gate import evaluate_boundary_ptb_gate
from tyrex_pm.runtime.z_gap_ptb_capture import (
    PTB_CAPTURE_WAITING,
    capture_ptb_at_boundary,
    capture_trace_to_payload,
)
from tyrex_pm.runtime.z_gap_session_runtime import SessionRuntimeHandle
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.signal_state_store import SignalStateStore
from tyrex_pm.state.wallet_store import WalletStore


def _meta(start: float | None = None) -> Btc5mMarketMetadata:
    s = start if start is not None else time.time() + 2.0
    return Btc5mMarketMetadata(
        market_id="btc_5m_20260716_1200",
        condition_id="0xabc",
        yes_token_id="111",
        no_token_id="222",
        event_start_ts=s,
        event_end_ts=s + 300,
        event_slug="btc-updown-5m-test",
        event_url="https://polymarket.com/event/test",
    )


def _handle(*, price: str | None = None, status: str = "pending", lag_ms: float | None = None) -> SessionRuntimeHandle:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    store = SignalStateStore()
    if price is not None:
        store.update_price_to_beat(
            price=Decimal(price),
            status=status,
            observed_ts=datetime.now(timezone.utc),
            lag_ms=lag_ms,
        )
    coord.signal_state = store
    return SessionRuntimeHandle(
        repo_root=Path("."),
        runs_dir=Path("."),
        run_id=MagicMock(),
        coord=coord,
        sink=MagicMock(),
        stop=MagicMock(),
        signal_feed_state=MagicMock(price_to_beat_tracker=None),
        oms=MagicMock(submit_count=0),
    )


@pytest.mark.asyncio
async def test_capture_waits_for_post_boundary_rtds_tick(tmp_path: Path) -> None:
    """RTDS tick arrives 200ms after boundary — capture should wait and lock K."""
    start = 1000.0
    meta = _meta(start=start)
    handle = _handle()
    clock = {"t": start - 0.1}
    tick_at = start + 0.2

    async def _sleep(dt: float) -> None:
        clock["t"] += dt

    async def _evaluate(_app, _meta, *, experimental_mode: bool = False):
        now = clock["t"]
        if now >= tick_at:
            handle.coord.signal_state.update_price_to_beat(
                Decimal("95000.00"),
                status=PTB_STATUS_OBSERVED,
                observed_ts=datetime.fromtimestamp(tick_at, tz=timezone.utc),
                lag_ms=200.0,
            )
        return evaluate_boundary_ptb_gate(
            market_id=meta.market_id,
            event_start_ts=meta.event_start_ts,
            event_end_ts=meta.event_end_ts,
            now_ts=now,
            live_price="95000.00" if now >= tick_at else None,
            live_status=PTB_STATUS_OBSERVED if now >= tick_at else PTB_STATUS_MISSING,
            live_lag_ms=200.0 if now >= tick_at else None,
            chainlink_log_path=tmp_path / "empty.jsonl",
            ptb_store_path=tmp_path / "ptb.json",
            artifacts_dir=tmp_path / "art",
            experimental_mode=True,
        )

    boundary, trace, _reading = await capture_ptb_at_boundary(
        handle=handle,
        meta=meta,
        ptb_config=ZGapPtbConfig(capture_wait_timeout_ms=5000, poll_interval_ms=50),
        evaluate_boundary=_evaluate,
        app=MagicMock(),
        experimental_mode=True,
        now_ts=lambda: clock["t"],
        sleep=_sleep,
    )
    assert trace.poll_count >= 2
    assert boundary.ready_to_evaluate
    assert trace.selected_k == "95000.00"
    assert trace.wait_duration_ms >= 200.0


@pytest.mark.asyncio
async def test_capture_at_exact_boundary(tmp_path: Path) -> None:
    start = 1000.0
    meta = _meta(start=start)
    handle = _handle(
        price="95100.00",
        status=PTB_STATUS_OBSERVED,
        lag_ms=0.0,
    )
    clock = {"t": start - 0.1}

    async def _sleep(dt: float) -> None:
        clock["t"] += dt

    async def _evaluate(_app, _meta, *, experimental_mode: bool = False):
        return evaluate_boundary_ptb_gate(
            market_id=meta.market_id,
            event_start_ts=meta.event_start_ts,
            event_end_ts=meta.event_end_ts,
            now_ts=clock["t"],
            live_price="95100.00",
            live_status=PTB_STATUS_OBSERVED,
            live_lag_ms=0.0,
            chainlink_log_path=tmp_path / "empty.jsonl",
            ptb_store_path=tmp_path / "ptb.json",
            artifacts_dir=tmp_path / "art",
            experimental_mode=True,
        )

    boundary, trace, _ = await capture_ptb_at_boundary(
        handle=handle,
        meta=meta,
        ptb_config=ZGapPtbConfig(capture_wait_timeout_ms=1000, poll_interval_ms=20),
        evaluate_boundary=_evaluate,
        app=MagicMock(),
        experimental_mode=True,
        now_ts=lambda: clock["t"],
        sleep=_sleep,
    )
    assert boundary.ready_to_evaluate
    assert trace.capture_outcome == "PTB_LOCKED"


@pytest.mark.asyncio
async def test_capture_near_timeout(tmp_path: Path) -> None:
    start = 1000.0
    meta = _meta(start=start)
    handle = _handle()
    clock = {"t": start - 0.1}
    log_path = tmp_path / "ticks.jsonl"
    tick_ts = datetime.fromtimestamp(start + 4.8, tz=timezone.utc).isoformat()
    written = {"done": False}

    async def _sleep(dt: float) -> None:
        clock["t"] += dt
        if clock["t"] >= start + 4.5 and not written["done"]:
            log_path.write_text(
                json.dumps({"source_ts": tick_ts, "price": "94999.50"}) + "\n",
                encoding="utf-8",
            )
            written["done"] = True

    async def _evaluate(_app, _meta, *, experimental_mode: bool = False):
        return evaluate_boundary_ptb_gate(
            market_id=meta.market_id,
            event_start_ts=meta.event_start_ts,
            event_end_ts=meta.event_end_ts,
            now_ts=clock["t"],
            live_price=None,
            live_status=PTB_STATUS_MISSING,
            chainlink_log_path=log_path,
            ptb_store_path=tmp_path / "ptb.json",
            artifacts_dir=tmp_path / "art",
            experimental_mode=True,
        )

    boundary, trace, _ = await capture_ptb_at_boundary(
        handle=handle,
        meta=meta,
        ptb_config=ZGapPtbConfig(capture_wait_timeout_ms=5000, poll_interval_ms=50),
        evaluate_boundary=_evaluate,
        app=MagicMock(),
        experimental_mode=True,
        now_ts=lambda: clock["t"],
        sleep=_sleep,
    )
    assert trace.wait_duration_ms >= 4500.0
    assert trace.selected_k == "94999.50"


@pytest.mark.asyncio
async def test_sidecar_fallback_when_no_rtds(tmp_path: Path) -> None:
    start = time.time() + 0.2
    meta = _meta(start=start)
    handle = _handle()
    log_path = tmp_path / "ticks.jsonl"
    tick_ts = datetime.fromtimestamp(start + 0.1, tz=timezone.utc).isoformat()
    log_path.write_text(
        json.dumps({"source_ts": tick_ts, "price": "94888.00"}) + "\n",
        encoding="utf-8",
    )

    async def _evaluate(_app, _meta, *, experimental_mode: bool = False):
        return evaluate_boundary_ptb_gate(
            market_id=meta.market_id,
            event_start_ts=meta.event_start_ts,
            event_end_ts=meta.event_end_ts,
            now_ts=time.time(),
            live_price=None,
            live_status=PTB_STATUS_MISSING,
            chainlink_log_path=log_path,
            ptb_store_path=tmp_path / "ptb.json",
            artifacts_dir=tmp_path / "art",
            experimental_mode=True,
        )

    boundary, trace, _ = await capture_ptb_at_boundary(
        handle=handle,
        meta=meta,
        ptb_config=ZGapPtbConfig(capture_wait_timeout_ms=500, poll_interval_ms=20),
        evaluate_boundary=_evaluate,
        app=MagicMock(),
        experimental_mode=True,
        now_ts=time.time,
        sleep=AsyncMock(),
    )
    assert boundary.ready_to_evaluate
    assert trace.selected_k == "94888.00"
    assert trace.selected_source == "log_boundary"


@pytest.mark.asyncio
async def test_missing_after_timeout(tmp_path: Path) -> None:
    start = time.time() + 0.15
    meta = _meta(start=start)
    handle = _handle()
    log_path = tmp_path / "empty.jsonl"

    async def _evaluate(_app, _meta, *, experimental_mode: bool = False):
        return evaluate_boundary_ptb_gate(
            market_id=meta.market_id,
            event_start_ts=meta.event_start_ts,
            event_end_ts=meta.event_end_ts,
            now_ts=time.time(),
            live_price=None,
            live_status=PTB_STATUS_MISSING,
            chainlink_log_path=log_path,
            ptb_store_path=tmp_path / "ptb.json",
            artifacts_dir=tmp_path / "art",
            experimental_mode=True,
        )

    boundary, trace, _ = await capture_ptb_at_boundary(
        handle=handle,
        meta=meta,
        ptb_config=ZGapPtbConfig(capture_wait_timeout_ms=300, poll_interval_ms=50),
        evaluate_boundary=_evaluate,
        app=MagicMock(),
        experimental_mode=True,
        now_ts=time.time,
        sleep=AsyncMock(),
    )
    assert not boundary.ready_to_evaluate
    assert trace.capture_outcome == "PTB_MISSING"
    assert trace.poll_count >= 2


def test_capture_trace_payload_fields() -> None:
    from tyrex_pm.runtime.z_gap_ptb_capture import PtbCaptureTrace

    trace = PtbCaptureTrace(
        event_start_ts=100.0,
        ptb_capture_started_at=100.0,
        ptb_capture_deadline=105.0,
        ptb_capture_ended_at=103.0,
        wait_duration_ms=3000.0,
        poll_count=4,
        live_candidate_k="95000",
        live_source_ts=100.1,
        live_receive_ts=100.2,
        live_boundary_lag_ms=100.0,
        sidecar_candidate_k=None,
        sidecar_source_ts=None,
        sidecar_boundary_lag_ms=None,
        selected_k="95000",
        selected_source="live_boundary",
        selected_boundary_lag_ms=100.0,
        ptb_status="PTB_LOCKED_READY_TO_EVALUATE",
        ptb_failure_reason=None,
        capture_outcome="PTB_LOCKED",
    )
    payload = capture_trace_to_payload(trace)
    assert payload["selected_k"] == "95000"
    assert payload["ptb_capture_deadline"]
    assert payload["capture_outcome"] == "PTB_LOCKED"


@pytest.mark.asyncio
async def test_reads_signal_state_store_not_injection() -> None:
    start = time.time() + 0.2
    meta = _meta(start=start)
    handle = _handle(
        price="95200.00",
        status=PTB_STATUS_OBSERVED,
        lag_ms=50.0,
    )

    async def _evaluate(_app, _meta, *, experimental_mode: bool = False):
        from tyrex_pm.runtime.z_gap_experimental import read_live_ptb_from_handle

        reading = read_live_ptb_from_handle(handle)
        return evaluate_boundary_ptb_gate(
            market_id=meta.market_id,
            event_start_ts=meta.event_start_ts,
            event_end_ts=meta.event_end_ts,
            now_ts=time.time(),
            live_price=reading.price,
            live_status=reading.status,
            live_lag_ms=reading.lag_ms,
            experimental_mode=True,
        )

    _boundary, _trace, reading = await capture_ptb_at_boundary(
        handle=handle,
        meta=meta,
        ptb_config=ZGapPtbConfig(capture_wait_timeout_ms=500, poll_interval_ms=20),
        evaluate_boundary=_evaluate,
        app=MagicMock(),
        experimental_mode=True,
        now_ts=time.time,
        sleep=AsyncMock(),
    )
    assert reading.price == "95200.00"


@pytest.mark.asyncio
async def test_k_locks_once(tmp_path: Path) -> None:
    start = time.time() + 0.2
    meta = _meta(start=start)
    handle = _handle()
    store_path = tmp_path / "ptb_store.json"

    async def _evaluate(_app, _meta, *, experimental_mode: bool = False):
        return evaluate_boundary_ptb_gate(
            market_id=meta.market_id,
            event_start_ts=meta.event_start_ts,
            event_end_ts=meta.event_end_ts,
            now_ts=time.time(),
            live_price="95001.00",
            live_status=PTB_STATUS_OBSERVED,
            live_lag_ms=80.0,
            ptb_store_path=store_path,
            artifacts_dir=tmp_path / "art",
            experimental_mode=True,
        )

    await capture_ptb_at_boundary(
        handle=handle,
        meta=meta,
        ptb_config=ZGapPtbConfig(capture_wait_timeout_ms=500, poll_interval_ms=20),
        evaluate_boundary=_evaluate,
        app=MagicMock(),
        experimental_mode=True,
        now_ts=time.time,
        sleep=AsyncMock(),
    )
    second = evaluate_boundary_ptb_gate(
        market_id=meta.market_id,
        event_start_ts=meta.event_start_ts,
        event_end_ts=meta.event_end_ts,
        now_ts=time.time(),
        live_price="99999.00",
        live_status=PTB_STATUS_OBSERVED,
        live_lag_ms=80.0,
        ptb_store_path=store_path,
        experimental_mode=True,
    )
    assert second.selection is not None
    assert second.selection.selected_k == "95001.00"


def test_waiting_status_constant() -> None:
    assert PTB_CAPTURE_WAITING == "WAITING_FOR_PTB"
