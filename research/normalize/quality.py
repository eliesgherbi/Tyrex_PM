"""Per-market quality scorecards (M2B.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from tyrex_pm.core.events import EventType, MarketEvent

from research.normalize.io import EventReadStats, iso_ts


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts)


def compute_stream_metrics(events: list[MarketEvent]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    recv_times: list[datetime] = []
    source_times: list[datetime] = []
    max_staleness_s = 0.0

    for event in events:
        et = event.event_type.value
        counts[et] = counts.get(et, 0) + 1
        recv_times.append(event.recv_ts)
        if event.source_ts is not None:
            source_times.append(event.source_ts)
            staleness = (event.recv_ts - event.source_ts).total_seconds()
            if staleness > max_staleness_s:
                max_staleness_s = staleness

    first_recv = min(recv_times) if recv_times else None
    last_recv = max(recv_times) if recv_times else None
    max_inter_event_gap_s = 0.0
    if len(recv_times) > 1:
        ordered = sorted(recv_times)
        for prev, cur in zip(ordered, ordered[1:]):
            gap = (cur - prev).total_seconds()
            if gap > max_inter_event_gap_s:
                max_inter_event_gap_s = gap

    recording_duration_s = None
    if first_recv and last_recv:
        recording_duration_s = (last_recv - first_recv).total_seconds()

    event_count = len(events)
    ws_seq_gap_count = counts.get(EventType.WS_SEQ_GAP.value, 0)
    gap_rate = (ws_seq_gap_count / event_count) if event_count else 0.0

    return {
        "event_count": event_count,
        "book_snapshot_count": counts.get(EventType.BOOK_SNAPSHOT.value, 0),
        "book_delta_count": counts.get(EventType.BOOK_DELTA.value, 0),
        "trade_price_count": counts.get(EventType.LAST_TRADE_PRICE.value, 0),
        "tick_size_change_count": counts.get(EventType.TICK_SIZE_CHANGE.value, 0),
        "best_bid_ask_count": counts.get(EventType.BEST_BID_ASK.value, 0),
        "market_resolved_count": counts.get(EventType.MARKET_RESOLVED.value, 0),
        "price_to_beat_count": counts.get(EventType.PRICE_TO_BEAT_OBSERVED.value, 0),
        "ws_seq_gap_count": ws_seq_gap_count,
        "market_discovered_count": counts.get(EventType.MARKET_DISCOVERED.value, 0),
        "gap_rate": gap_rate,
        "first_recv_ts": iso_ts(first_recv),
        "last_recv_ts": iso_ts(last_recv),
        "max_inter_event_gap_s": max_inter_event_gap_s,
        "max_staleness_s": max_staleness_s,
        "recording_duration_s": recording_duration_s,
    }


def coverage_status_for_market(
    market_id: str,
    coverage: dict[str, Any] | None,
    *,
    manifest_event_count: int,
) -> tuple[str, float | None]:
    if coverage is None:
        if manifest_event_count > 0:
            return "recorded", None
        return "partial", None
    for row in coverage.get("markets") or []:
        if row.get("market_id") != market_id:
            continue
        status = str(row.get("status", "partial"))
        pct = coverage.get("coverage_pct")
        return status, float(pct) if pct is not None else None
    return "partial", coverage.get("coverage_pct")


def derive_status(
    *,
    coverage_status: str,
    corrupt_row_count: int,
    event_count: int,
) -> str:
    if corrupt_row_count > 0 and event_count == 0:
        return "corrupt"
    if coverage_status in {"skipped", "missing"}:
        return coverage_status
    if event_count == 0:
        return "partial"
    return coverage_status if coverage_status else "recorded"
