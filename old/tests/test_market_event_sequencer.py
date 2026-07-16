"""MarketSequencer ordering, dedup, and gap tests (M2B.0-A)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tyrex_pm.core.events import (
    EventType,
    MarketEvent,
    compute_event_id,
    compute_payload_digest,
)
from tyrex_pm.core.ids import TokenId
from tyrex_pm.ingestion.sequencer import MarketSequencer

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "events"


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def _event_from_fixture_row(row: dict) -> MarketEvent:
    payload = row.get("payload") or {}
    digest = compute_payload_digest(payload)
    token_raw = row.get("token_id")
    token_id = TokenId(str(token_raw)) if token_raw else None
    event_type = EventType(str(row["event_type"]))
    venue_cursor = row.get("venue_cursor")
    source_ts = _dt(row["source_ts"]) if row.get("source_ts") else None
    recv_ts = _dt(row["recv_ts"])
    event_id = compute_event_id(
        event_type=event_type.value,
        token_id=str(token_id) if token_id else None,
        venue_cursor=venue_cursor,
        source_ts=source_ts,
        payload_digest=digest,
    )
    return MarketEvent(
        event_id=event_id,
        event_type=event_type,
        market_id=row.get("market_id"),
        token_id=token_id,
        venue_cursor=venue_cursor,
        source_ts=source_ts,
        recv_ts=recv_ts,
        payload=payload,
        connection_id=row.get("connection_id"),
        local_counter=row.get("local_counter"),
    )


def _load_fixture(name: str) -> list[MarketEvent]:
    path = FIXTURES / name
    events: list[MarketEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        events.append(_event_from_fixture_row(json.loads(line)))
    return events


def test_in_order_hashes_forwarded() -> None:
    seq = MarketSequencer("m1")
    seen_types: list[EventType] = []
    for ev in _load_fixture("ws_book_sequence.jsonl"):
        out = seq.ingest(ev)
        seen_types.extend(e.event_type for e in out)
    assert seen_types == [EventType.BOOK_SNAPSHOT, EventType.BOOK_DELTA, EventType.BOOK_DELTA]


def test_duplicate_hash_dropped() -> None:
    seq = MarketSequencer("m1")
    ev = _event_from_fixture_row(
        {
            "event_type": "book_snapshot",
            "token_id": "tok-dup",
            "venue_cursor": "0xdup",
            "source_ts": "2026-07-03T12:00:00+00:00",
            "recv_ts": "2026-07-03T12:00:00+00:00",
            "payload": {"raw": {}},
        }
    )
    assert len(seq.ingest(ev)) == 1
    dup = MarketEvent(
        event_id="different-id-but-same-hash",
        event_type=EventType.BOOK_DELTA,
        market_id=None,
        token_id=TokenId("tok-dup"),
        venue_cursor="0xdup",
        source_ts=_dt("2026-07-03T12:00:01+00:00"),
        recv_ts=_dt("2026-07-03T12:00:01+00:00"),
        payload={"raw": {"other": True}},
    )
    assert seq.ingest(dup) == []


def test_duplicate_event_id_dropped() -> None:
    seq = MarketSequencer("m1")
    ev = _event_from_fixture_row(
        {
            "event_type": "book_snapshot",
            "token_id": "tok-id",
            "venue_cursor": "0x111",
            "source_ts": "2026-07-03T12:00:00+00:00",
            "recv_ts": "2026-07-03T12:00:00+00:00",
            "payload": {"raw": {}},
        }
    )
    assert seq.ingest(ev) == [ev]
    assert seq.ingest(ev) == []


def test_out_of_order_emits_ws_seq_gap() -> None:
    seq = MarketSequencer("m1")
    rows = _load_fixture("ws_gap_sequence.jsonl")
    first = seq.ingest(rows[0])
    assert len(first) == 1
    assert first[0].event_type == EventType.BOOK_SNAPSHOT

    second = seq.ingest(rows[1])
    assert len(second) == 1
    gap = second[0]
    assert gap.event_type == EventType.WS_SEQ_GAP
    assert gap.payload["last_venue_cursor"] == "0xbbb"
    assert gap.payload["received_venue_cursor"] == "0xaaa"
    assert gap.payload["reason"] == "out_of_order"


def test_no_cursor_always_forwards() -> None:
    seq = MarketSequencer("m1")
    ev = MarketEvent(
        event_id="no-cursor-1",
        event_type=EventType.BOOK_DELTA,
        market_id="m1",
        token_id=TokenId("tok-nc"),
        venue_cursor=None,
        source_ts=_dt("2026-07-03T12:00:00+00:00"),
        recv_ts=_dt("2026-07-03T12:00:00+00:00"),
        payload={"raw": {}},
    )
    assert seq.ingest(ev) == [ev]


def test_reorder_buffer_holds_inverted_ts() -> None:
    seq = MarketSequencer("m1")
    later = _event_from_fixture_row(
        {
            "event_type": "trade_print",
            "token_id": "tok-rb",
            "venue_cursor": None,
            "source_ts": "2026-07-03T12:00:02+00:00",
            "recv_ts": "2026-07-03T12:00:02+00:00",
            "payload": {"price": "0.6"},
        }
    )
    earlier = _event_from_fixture_row(
        {
            "event_type": "trade_print",
            "token_id": "tok-rb",
            "venue_cursor": None,
            "source_ts": "2026-07-03T12:00:01+00:00",
            "recv_ts": "2026-07-03T12:00:01+00:00",
            "payload": {"price": "0.5"},
        }
    )
    assert seq.ingest(later) == [later]
    assert seq.ingest(earlier) == []
    flushed = seq.flush()
    assert len(flushed) == 1
    assert flushed[0].event_id == earlier.event_id


def test_flush_drains_buffer() -> None:
    seq = MarketSequencer("m1")
    held = _event_from_fixture_row(
        {
            "event_type": "trade_print",
            "token_id": "tok-fl",
            "venue_cursor": None,
            "source_ts": "2026-07-03T12:00:01+00:00",
            "recv_ts": "2026-07-03T12:00:01+00:00",
            "payload": {},
        }
    )
    anchor = _event_from_fixture_row(
        {
            "event_type": "trade_print",
            "token_id": "tok-fl",
            "venue_cursor": None,
            "source_ts": "2026-07-03T12:00:02+00:00",
            "recv_ts": "2026-07-03T12:00:02+00:00",
            "payload": {},
        }
    )
    seq.ingest(anchor)
    seq.ingest(held)
    out = seq.flush()
    assert len(out) == 1
    assert out[0].source_ts == held.source_ts
