"""EventSink manifest and JSONL round-trip tests (M2B.1-A)."""

from __future__ import annotations

import json

import pytest

from tyrex_pm.core.events import EventType, MarketEvent, compute_event_id, compute_payload_digest, event_from_dict
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.reporting.event_sink import EventSink


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
async def test_manifest_tracks_segments_and_event_types(tmp_path) -> None:
    out = tmp_path / "rec"
    sink = EventSink(
        out,
        "btc_5m_record_single",
        yes_token_id="yes",
        no_token_id="no",
        git_sha="testsha",
        scenario="record_btc5m_single.yaml",
        batch_size=2,
        batch_flush_ms=5,
    )
    await sink.start()
    for i in range(5):
        sink.emit(_event(i))
    await sink.stop()

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["market_id"] == "btc_5m_record_single"
    assert manifest["yes_token_id"] == "yes"
    assert manifest["no_token_id"] == "no"
    assert manifest["git_sha"] == "testsha"
    assert manifest["scenario"] == "record_btc5m_single.yaml"
    assert manifest["recording_started_ts"]
    assert manifest["recording_ended_ts"]
    assert manifest["event_types_seen"].get("book_snapshot", 0) == 5
    assert sum(s["event_count"] for s in manifest["segments"]) == 5

    seg = out / "events-00001.jsonl"
    assert seg.exists()
    for line in seg.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        roundtrip = event_from_dict(json.loads(line))
        assert roundtrip.event_type == EventType.BOOK_SNAPSHOT
