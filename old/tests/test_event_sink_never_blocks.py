"""EventSink non-blocking contract tests (M2B.1-A)."""

from __future__ import annotations

import asyncio
import time

import pytest

from tyrex_pm.core.events import EventType, MarketEvent, compute_event_id, compute_payload_digest
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.reporting.event_sink import EventSink


def _event(i: int) -> MarketEvent:
    payload = {"raw": {"event_type": "book", "i": i}}
    digest = compute_payload_digest(payload)
    recv = utc_now()
    return MarketEvent(
        event_id=compute_event_id(
            event_type=EventType.BOOK_SNAPSHOT.value,
            token_id="tok",
            venue_cursor=f"h{i}",
            source_ts=None,
            payload_digest=digest,
        ),
        event_type=EventType.BOOK_SNAPSHOT,
        market_id="m1",
        token_id=TokenId("tok"),
        venue_cursor=f"h{i}",
        source_ts=None,
        recv_ts=recv,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_emit_returns_without_awaiting_disk(tmp_path, monkeypatch) -> None:
    sink = EventSink(tmp_path / "rec", "m1", batch_flush_ms=10_000, queue_maxsize=500)
    slow = asyncio.Event()

    async def _slow_loop() -> None:
        slow.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(sink, "_writer_loop", _slow_loop)
    await sink.start()
    await asyncio.wait_for(slow.wait(), timeout=1.0)

    t0 = time.perf_counter()
    for i in range(200):
        sink.emit(_event(i))
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.25
    sink._stopped = True
    if sink._writer_task is not None:
        sink._writer_task.cancel()
        try:
            await sink._writer_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_emit_only_enqueues_before_writer_flushes(tmp_path) -> None:
    sink = EventSink(tmp_path / "rec", "m1", batch_flush_ms=60_000, queue_maxsize=50)
    await sink.start()
    sink.emit(_event(1))
    assert sink._queue is not None
    assert sink._queue.qsize() == 1
    await sink.stop()
