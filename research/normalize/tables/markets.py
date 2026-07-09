"""markets and ws_quality table rows (M2B.3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from research.normalize.quality import compute_stream_metrics, coverage_status_for_market, derive_status


def build_market_row(
    *,
    date: str,
    market_id: str,
    manifest: dict[str, Any],
    manifest_path: Path,
    stream_metrics: dict[str, Any],
    coverage: dict[str, Any] | None,
    corrupt_row_count: int,
    duplicate_event_count: int,
) -> dict[str, Any]:
    segments = manifest.get("segments") or []
    event_count = sum(int(s.get("event_count", 0)) for s in segments)
    coverage_status, coverage_pct = coverage_status_for_market(market_id, coverage, manifest_event_count=event_count)
    event_start_ts = None
    event_end_ts = None
    event_slug = None
    return {
        "date": date,
        "market_id": market_id,
        "event_slug": event_slug,
        "yes_token_id": manifest.get("yes_token_id"),
        "no_token_id": manifest.get("no_token_id"),
        "recording_started_ts": manifest.get("recording_started_ts"),
        "recording_ended_ts": manifest.get("recording_ended_ts"),
        "event_start_ts": event_start_ts,
        "event_end_ts": event_end_ts,
        "event_count": stream_metrics.get("event_count", event_count),
        "segment_count": len(segments),
        "compressed": bool(manifest.get("compressed", False)),
        "dropped_events": int(manifest.get("dropped_events", 0)),
        "coverage_status": coverage_status,
        "coverage_pct": coverage_pct,
        "first_recv_ts": stream_metrics.get("first_recv_ts"),
        "last_recv_ts": stream_metrics.get("last_recv_ts"),
        "book_snapshot_count": stream_metrics.get("book_snapshot_count", 0),
        "book_delta_count": stream_metrics.get("book_delta_count", 0),
        "ws_seq_gap_count": stream_metrics.get("ws_seq_gap_count", 0),
        "market_discovered_count": stream_metrics.get("market_discovered_count", 0),
        "max_staleness_s": stream_metrics.get("max_staleness_s", 0.0),
        "duplicate_event_count": duplicate_event_count,
        "corrupt_row_count": corrupt_row_count,
        "source_manifest_path": str(manifest_path),
        "price_to_beat": None,
        "price_to_beat_ts": None,
        "price_to_beat_source": None,
        "price_to_beat_lag_ms": None,
        "final_reference_price": None,
        "final_reference_price_ts": None,
        "final_reference_lag_ms": None,
        "direction_vs_price_to_beat": None,
        "winning_asset_id": None,
        "winning_outcome": None,
        "resolved_ts": None,
        "trade_price_count": stream_metrics.get("trade_price_count", 0),
        "tick_size_change_count": stream_metrics.get("tick_size_change_count", 0),
        "best_bid_ask_count": stream_metrics.get("best_bid_ask_count", 0),
        "reference_tick_count": 0,
    }


def enrich_market_row_from_discovered(row: dict[str, Any], events_payload: dict[str, Any] | None) -> dict[str, Any]:
    if not events_payload:
        return row
    row = dict(row)
    row["event_slug"] = events_payload.get("event_slug")
    est = events_payload.get("event_start_ts")
    eet = events_payload.get("event_end_ts")
    if est is not None:
        from datetime import datetime, timezone

        row["event_start_ts"] = datetime.fromtimestamp(float(est), tz=timezone.utc).isoformat()
    if eet is not None:
        from datetime import datetime, timezone

        row["event_end_ts"] = datetime.fromtimestamp(float(eet), tz=timezone.utc).isoformat()
    return row


def enrich_market_row_from_events(row: dict[str, Any], events: list) -> dict[str, Any]:
    from tyrex_pm.core.events import EventType

    row = dict(row)
    ptb_event = None
    resolution_event = None
    for event in reversed(events):
        if ptb_event is None and event.event_type == EventType.PRICE_TO_BEAT_OBSERVED:
            ptb_event = event
        if resolution_event is None and event.event_type == EventType.MARKET_RESOLVED:
            resolution_event = event
    if ptb_event is not None:
        p = ptb_event.payload
        row["price_to_beat"] = p.get("price_to_beat")
        row["price_to_beat_ts"] = p.get("price_to_beat_ts")
        row["price_to_beat_source"] = p.get("price_to_beat_source")
        row["price_to_beat_lag_ms"] = p.get("price_to_beat_lag_ms")
        row["final_reference_price"] = p.get("final_reference_price")
        row["final_reference_price_ts"] = p.get("final_reference_price_ts")
        row["final_reference_lag_ms"] = p.get("final_reference_lag_ms")
        row["direction_vs_price_to_beat"] = p.get("direction_vs_price_to_beat")
    if resolution_event is not None:
        raw = resolution_event.payload.get("raw") or resolution_event.payload
        if isinstance(raw, dict):
            row["winning_asset_id"] = raw.get("winning_asset_id")
            row["winning_outcome"] = raw.get("winning_outcome")
            row["resolved_ts"] = (
                resolution_event.source_ts.isoformat() if resolution_event.source_ts else None
            )
    return row


def build_ws_quality_row(
    *,
    date: str,
    market_id: str,
    manifest: dict[str, Any],
    stream_metrics: dict[str, Any],
    corrupt_row_count: int,
    duplicate_event_count: int,
    coverage_status: str,
) -> dict[str, Any]:
    dropped = int(manifest.get("dropped_events", 0))
    status = derive_status(
        coverage_status=coverage_status,
        corrupt_row_count=corrupt_row_count,
        event_count=int(stream_metrics.get("event_count", 0)),
    )
    return {
        "date": date,
        "market_id": market_id,
        "event_count": stream_metrics.get("event_count", 0),
        "ws_seq_gap_count": stream_metrics.get("ws_seq_gap_count", 0),
        "gap_rate": stream_metrics.get("gap_rate", 0.0),
        "duplicate_event_count": duplicate_event_count,
        "dropped_events": dropped,
        "corrupt_row_count": corrupt_row_count,
        "first_recv_ts": stream_metrics.get("first_recv_ts"),
        "last_recv_ts": stream_metrics.get("last_recv_ts"),
        "max_inter_event_gap_s": stream_metrics.get("max_inter_event_gap_s", 0.0),
        "max_staleness_s": stream_metrics.get("max_staleness_s", 0.0),
        "recording_duration_s": stream_metrics.get("recording_duration_s"),
        "status": status,
    }
