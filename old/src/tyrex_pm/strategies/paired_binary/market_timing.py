"""BTC 5m market timing diagnostics (Phase 2 observability only)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

MARKET_TIMING_PRE_START = "pre_start"
MARKET_TIMING_ACTIVE = "active"
MARKET_TIMING_NEAR_CLOSE = "near_close"
MARKET_TIMING_CLOSED = "closed"
MARKET_TIMING_UNKNOWN = "unknown"

TIMING_SOURCE_SCENARIO = "scenario_metadata"
TIMING_SOURCE_MARKET_API = "market_metadata_api"
TIMING_SOURCE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class MarketTimingSnapshot:
    market_id: str
    condition_id: str | None
    yes_token_id: str
    no_token_id: str
    event_start_ts: float | None
    event_end_ts: float | None
    now_ts: float
    phase: str
    seconds_to_start: float | None
    seconds_to_close: float | None
    near_close_window_s: float
    source: str


def _utc_now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def classify_market_timing_phase(
    *,
    now_ts: float,
    event_start_ts: float | None,
    event_end_ts: float | None,
    near_close_window_s: float,
) -> tuple[str, float | None, float | None]:
    if event_start_ts is None or event_end_ts is None:
        return MARKET_TIMING_UNKNOWN, None, None
    seconds_to_start = event_start_ts - now_ts
    seconds_to_close = event_end_ts - now_ts
    near_close_start = event_end_ts - near_close_window_s
    if now_ts < event_start_ts:
        return MARKET_TIMING_PRE_START, seconds_to_start, seconds_to_close
    if now_ts < near_close_start:
        return MARKET_TIMING_ACTIVE, seconds_to_start, seconds_to_close
    if now_ts < event_end_ts:
        return MARKET_TIMING_NEAR_CLOSE, seconds_to_start, seconds_to_close
    return MARKET_TIMING_CLOSED, seconds_to_start, seconds_to_close


def _parse_ts_from_raw(raw: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        val = raw.get(key)
        if val in (None, ""):
            continue
        if isinstance(val, (int, float)):
            return float(val)
        text = str(val).strip()
        if text.isdigit():
            return float(text)
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
    return None


def resolve_market_timing_metadata(
    *,
    market_id: str,
    yes_token_id: str,
    no_token_id: str,
    condition_id: str | None,
    event_start_ts: float | None,
    event_end_ts: float | None,
    coord: object | None = None,
) -> tuple[str | None, float | None, float | None, str]:
    src = TIMING_SOURCE_UNKNOWN
    cid = condition_id
    start = event_start_ts
    end = event_end_ts
    if start is not None and end is not None:
        src = TIMING_SOURCE_SCENARIO
        return cid, start, end, src

    cache = getattr(coord, "market_info_cache", None) if coord is not None else None
    if cache is not None:
        snapshot = cache.snapshot() if hasattr(cache, "snapshot") else {}
        for tid in (yes_token_id, no_token_id):
            info = snapshot.get(tid) if isinstance(snapshot, dict) else None
            if info is None and hasattr(snapshot, "get"):
                from tyrex_pm.core.ids import TokenId

                info = snapshot.get(TokenId(tid))
            if info is None:
                continue
            if cid is None and getattr(info, "condition_id", None):
                cid = str(info.condition_id)
            raw = getattr(info, "raw", None) or {}
            if isinstance(raw, dict):
                if start is None:
                    start = _parse_ts_from_raw(
                        raw,
                        "event_start_ts",
                        "game_start_time",
                        "start_date_iso",
                        "startDate",
                    )
                if end is None:
                    end = _parse_ts_from_raw(
                        raw,
                        "event_end_ts",
                        "end_date_iso",
                        "endDate",
                        "end_date",
                    )
            if start is not None and end is not None:
                src = TIMING_SOURCE_MARKET_API
                break
    return cid, start, end, src


def build_market_timing_snapshot(
    *,
    market_id: str,
    yes_token_id: str,
    no_token_id: str,
    condition_id: str | None,
    event_start_ts: float | None,
    event_end_ts: float | None,
    near_close_window_s: float,
    coord: object | None = None,
    now_ts: float | None = None,
) -> MarketTimingSnapshot:
    now = _utc_now_ts() if now_ts is None else now_ts
    cid, start, end, source = resolve_market_timing_metadata(
        market_id=market_id,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        condition_id=condition_id,
        event_start_ts=event_start_ts,
        event_end_ts=event_end_ts,
        coord=coord,
    )
    phase, seconds_to_start, seconds_to_close = classify_market_timing_phase(
        now_ts=now,
        event_start_ts=start,
        event_end_ts=end,
        near_close_window_s=near_close_window_s,
    )
    return MarketTimingSnapshot(
        market_id=market_id,
        condition_id=cid,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        event_start_ts=start,
        event_end_ts=end,
        now_ts=now,
        phase=phase,
        seconds_to_start=seconds_to_start,
        seconds_to_close=seconds_to_close,
        near_close_window_s=near_close_window_s,
        source=source if phase != MARKET_TIMING_UNKNOWN else TIMING_SOURCE_UNKNOWN,
    )
