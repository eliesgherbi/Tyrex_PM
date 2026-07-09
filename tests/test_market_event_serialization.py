"""MarketEvent serialization and event_id determinism tests (M2B.0-A)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.events import (
    EventType,
    MarketEvent,
    canonical_payload_json,
    compute_event_id,
    compute_payload_digest,
    event_from_dict,
    event_to_dict,
)
from tyrex_pm.core.ids import TokenId


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def _sample_event(**overrides) -> MarketEvent:
    payload = overrides.pop("payload", {"raw": {"side": "BUY", "price": "0.5"}})
    digest = compute_payload_digest(payload)
    event_type = overrides.pop("event_type", EventType.BOOK_SNAPSHOT)
    token_id = overrides.pop("token_id", TokenId("tok-ser"))
    venue_cursor = overrides.pop("venue_cursor", "0xabc")
    source_ts = overrides.pop("source_ts", _dt("2026-07-03T12:00:00+00:00"))
    recv_ts = overrides.pop("recv_ts", _dt("2026-07-03T12:00:00.001+00:00"))
    event_id = overrides.pop(
        "event_id",
        compute_event_id(
            event_type=event_type.value,
            token_id=str(token_id),
            venue_cursor=venue_cursor,
            source_ts=source_ts,
            payload_digest=digest,
        ),
    )
    return MarketEvent(
        event_id=event_id,
        event_type=event_type,
        market_id=overrides.pop("market_id", "m1"),
        token_id=token_id,
        venue_cursor=venue_cursor,
        source_ts=source_ts,
        recv_ts=recv_ts,
        payload=payload,
        **overrides,
    )


def test_event_id_deterministic() -> None:
    ev1 = _sample_event()
    ev2 = _sample_event()
    assert ev1.event_id == ev2.event_id
    assert len(ev1.event_id) == 32


def test_event_id_changes_when_payload_changes() -> None:
    a = _sample_event(payload={"raw": {"n": 1}})
    b = _sample_event(payload={"raw": {"n": 2}})
    assert a.event_id != b.event_id


def test_payload_digest_stable_decimal_and_enum() -> None:
    payload = {"price": Decimal("0.55"), "kind": EventType.BOOK_DELTA}
    d1 = compute_payload_digest(payload)
    d2 = compute_payload_digest({"kind": EventType.BOOK_DELTA, "price": Decimal("0.55")})
    assert d1 == d2
    text = canonical_payload_json(payload)
    assert '"kind":"book_delta"' in text
    assert '"price":"0.55"' in text


def test_json_roundtrip_golden() -> None:
    ev = _sample_event(
        connection_id="conn-1",
        local_counter=7,
        payload={"raw": {"event_type": "book", "bids": [{"price": "0.5", "size": "10"}]}},
    )
    restored = event_from_dict(event_to_dict(ev))
    assert restored == ev


def test_all_event_types_serializable() -> None:
    for et in EventType:
        ev = _sample_event(event_type=et, venue_cursor=None if et == EventType.WS_SEQ_GAP else "0x1")
        roundtrip = event_from_dict(event_to_dict(ev))
        assert roundtrip.event_type == et
        assert roundtrip.event_id == ev.event_id


def test_token_id_roundtrip_null() -> None:
    ev = _sample_event(token_id=None, venue_cursor=None)
    data = event_to_dict(ev)
    assert data["token_id"] is None
    restored = event_from_dict(data)
    assert restored.token_id is None
