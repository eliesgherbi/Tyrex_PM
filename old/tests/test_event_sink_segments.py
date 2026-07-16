"""EventSink segment rotation tests (M2B.1-A)."""

from __future__ import annotations

import pytest

from tyrex_pm.core.events import EventType, MarketEvent, compute_event_id, compute_payload_digest
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.reporting.event_sink import EventSink


def _event(i: int, *, etype: EventType = EventType.BOOK_SNAPSHOT) -> MarketEvent:
    payload = {"raw": {"event_type": "book", "i": i}}
    digest = compute_payload_digest(payload)
    recv = utc_now()
    return MarketEvent(
        event_id=compute_event_id(
            event_type=etype.value,
            token_id="tok",
            venue_cursor=f"h{i}",
            source_ts=None,
            payload_digest=digest,
        ),
        event_type=etype,
        market_id="m1",
        token_id=TokenId("tok"),
        venue_cursor=f"h{i}",
        source_ts=None,
        recv_ts=recv,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_segment_rotation_creates_second_file(tmp_path) -> None:
    out = tmp_path / "rec"
    sink = EventSink(out, "m1", segment_max_mb=64, segment_max_s=300, batch_size=1, batch_flush_ms=1)
    sink._segment_max_bytes = 256
    await sink.start()
    for i in range(20):
        sink.emit(_event(i))
    await sink.stop()

    assert (out / "events-00001.jsonl").exists()
    assert (out / "events-00002.jsonl").exists()
    assert len(sink.manifest.segments) >= 2
