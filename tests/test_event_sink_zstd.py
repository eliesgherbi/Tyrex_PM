"""EventSink zstd compression tests (M2B.1-B)."""

from __future__ import annotations

import json

import pytest

from tyrex_pm.core.events import EventType, MarketEvent, compute_event_id, compute_payload_digest, event_from_dict
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.reporting.event_sink import EventSink, read_event_segment_lines

zstandard = pytest.importorskip("zstandard")


def _event(i: int) -> MarketEvent:
    payload = {"raw": {"event_type": "book", "asset_id": "tok", "i": i}}
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
async def test_compressed_segment_round_trip(tmp_path) -> None:
    out = tmp_path / "rec"
    sink = EventSink(out, "m1", compress=True, batch_size=2, batch_flush_ms=5)
    await sink.start()
    for i in range(4):
        sink.emit(_event(i))
    await sink.stop()

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["compressed"] is True
    seg_path = out / manifest["segments"][0]["path"]
    assert seg_path.name.endswith(".jsonl.zst")
    lines = read_event_segment_lines(seg_path)
    assert len(lines) == 4
    for line in lines:
        event_from_dict(json.loads(line))


@pytest.mark.asyncio
async def test_plain_jsonl_still_works_without_compress(tmp_path) -> None:
    out = tmp_path / "rec"
    sink = EventSink(out, "m1", compress=False, batch_size=1, batch_flush_ms=1)
    await sink.start()
    sink.emit(_event(0))
    await sink.stop()
    seg = out / "events-00001.jsonl"
    assert seg.exists()
    assert not seg.name.endswith(".zst")


@pytest.mark.asyncio
async def test_emit_non_blocking_with_compression(tmp_path, monkeypatch) -> None:
    import asyncio
    import time

    sink = EventSink(tmp_path / "rec", "m1", compress=True, batch_flush_ms=10_000, queue_maxsize=500)
    slow = asyncio.Event()

    async def _slow_loop() -> None:
        slow.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(sink, "_writer_loop", _slow_loop)
    await sink.start()
    await asyncio.wait_for(slow.wait(), timeout=1.0)

    t0 = time.perf_counter()
    for i in range(100):
        sink.emit(_event(i))
    assert time.perf_counter() - t0 < 0.25
    sink._stopped = True
    if sink._writer_task is not None:
        sink._writer_task.cancel()
        try:
            await sink._writer_task
        except asyncio.CancelledError:
            pass
