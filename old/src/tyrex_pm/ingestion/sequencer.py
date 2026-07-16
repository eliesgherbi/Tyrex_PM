"""Per-market event sequencer (Phase 2B M2B.0-A).

Orders, deduplicates, and detects sequence gaps for :class:`~tyrex_pm.core.events.MarketEvent`
streams. Polymarket ``venue_cursor`` uses lexicographic ``book_hash`` comparison —
mirrors ``market_ws_ingest._handle_sequence`` semantics.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

from tyrex_pm.core.events import (
    EventType,
    MarketEvent,
    compute_event_id,
    compute_payload_digest,
)
from tyrex_pm.core.ids import TokenId

__all__ = ["MarketSequencer"]

_BOOK_TYPES = frozenset({EventType.BOOK_SNAPSHOT, EventType.BOOK_DELTA})


def _source_ms(ts: datetime | None) -> int:
    if ts is None:
        return 0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1000)


def _recv_ms(ts: datetime) -> int:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1000)


class MarketSequencer:
    """Per-market sequencer for paired YES/NO token streams."""

    def __init__(
        self,
        market_id: str,
        *,
        reorder_buffer_ms: float = 75.0,
        dedup_capacity: int = 4096,
    ) -> None:
        self._market_id = market_id
        self._reorder_buffer_ms = max(0.0, reorder_buffer_ms)
        self._dedup: deque[str] = deque(maxlen=max(1, dedup_capacity))
        self._dedup_set: set[str] = set()
        self._last_cursor: dict[str, str] = {}
        self._pending: list[MarketEvent] = []
        self._emitted_max_source_ms: int | None = None

    @property
    def market_id(self) -> str:
        return self._market_id

    def ingest(self, event: MarketEvent) -> list[MarketEvent]:
        if event.event_id in self._dedup_set:
            return []
        self._remember_event_id(event.event_id)

        cursor_outcome = self._apply_venue_cursor(event)
        if cursor_outcome == "drop":
            return []
        if isinstance(cursor_outcome, MarketEvent):
            return [cursor_outcome]

        self._pending.append(event)
        return self._drain_pending()

    def flush(self) -> list[MarketEvent]:
        if not self._pending:
            return []
        self._pending.sort(key=self._sort_key)
        out = list(self._pending)
        self._pending.clear()
        for ev in out:
            self._emitted_max_source_ms = self._max_source(self._emitted_max_source_ms, ev)
        return out

    def _remember_event_id(self, event_id: str) -> None:
        if event_id in self._dedup_set:
            return
        if len(self._dedup) == self._dedup.maxlen:
            oldest = self._dedup[0]
            self._dedup_set.discard(oldest)
        self._dedup.append(event_id)
        self._dedup_set.add(event_id)

    def _apply_venue_cursor(self, event: MarketEvent) -> str | MarketEvent:
        if event.event_type not in _BOOK_TYPES:
            return "forward"
        token_key = str(event.token_id) if event.token_id is not None else None
        if token_key is None:
            return "forward"

        cursor = event.venue_cursor
        if cursor is None:
            return "forward"

        last = self._last_cursor.get(token_key)
        if last is not None and cursor == last:
            return "drop"
        if last is not None and cursor < last:
            return self._synthesize_gap(event, token_key=token_key, last=last, received=cursor)

        self._last_cursor[token_key] = cursor
        return "forward"

    def _synthesize_gap(
        self,
        event: MarketEvent,
        *,
        token_key: str,
        last: str,
        received: str,
    ) -> MarketEvent:
        payload = {
            "token_id": token_key,
            "last_venue_cursor": last,
            "received_venue_cursor": received,
            "reason": "out_of_order",
        }
        digest = compute_payload_digest(payload)
        gap_id = compute_event_id(
            event_type=EventType.WS_SEQ_GAP.value,
            token_id=token_key,
            venue_cursor=received,
            source_ts=event.source_ts,
            payload_digest=digest,
        )
        return MarketEvent(
            event_id=gap_id,
            event_type=EventType.WS_SEQ_GAP,
            market_id=event.market_id or self._market_id,
            token_id=TokenId(token_key),
            venue_cursor=received,
            source_ts=event.source_ts,
            recv_ts=event.recv_ts,
            payload=payload,
            schema_version=event.schema_version,
            connection_id=event.connection_id,
            local_counter=event.local_counter,
        )

    def _sort_key(self, event: MarketEvent) -> tuple[int, int, str]:
        return (_source_ms(event.source_ts), _recv_ms(event.recv_ts), event.event_id)

    def _drain_pending(self) -> list[MarketEvent]:
        if not self._pending:
            return []
        self._pending.sort(key=self._sort_key)
        out: list[MarketEvent] = []
        remaining: list[MarketEvent] = []
        for ev in self._pending:
            src_ms = _source_ms(ev.source_ts)
            if self._emitted_max_source_ms is not None and src_ms < self._emitted_max_source_ms:
                remaining.append(ev)
                continue
            out.append(ev)
            self._emitted_max_source_ms = self._max_source(self._emitted_max_source_ms, ev)
        self._pending = remaining
        return out

    @staticmethod
    def _max_source(current: int | None, event: MarketEvent) -> int:
        src = _source_ms(event.source_ts)
        if current is None:
            return src
        return max(current, src)
