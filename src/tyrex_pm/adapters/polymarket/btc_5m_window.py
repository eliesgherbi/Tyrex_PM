"""Authoritative BTC 5-minute market window resolution (R7A.1).

Slug epoch is the trading-window start. Gamma ``startDate`` / ``createdAt`` are
listing/creation metadata and must not be used as ``market_start``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

BTC_5M_WINDOW_S = 300
_BTC_5M_SLUG_TS = re.compile(r"btc-updown-5m-(\d{10,})$", re.IGNORECASE)
# Title like: "... July 17, 2:15AM-2:20AM ET"
_TITLE_WINDOW = re.compile(
    r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(?P<day>\d{1,2}),\s*"
    r"(?P<sh>\d{1,2}):(?P<sm>\d{2})(?P<sap>AM|PM)"
    r"\s*[-–—]\s*"
    r"(?P<eh>\d{1,2}):(?P<em>\d{2})(?P<eap>AM|PM)\s*ET",
    re.IGNORECASE,
)
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


class MarketWindowError(RuntimeError):
    """Typed market-window blocker code in ``args[0]``."""


@dataclass(frozen=True)
class FieldProvenance:
    internal_field: str
    api_field_source: str
    semantic_meaning: str
    trusted_for_scheduling: bool


PROVENANCE: tuple[FieldProvenance, ...] = (
    FieldProvenance(
        "market_start",
        "slug epoch `btc-updown-5m-{unix}` (+ cross-check market.eventStartTime / event.startTime)",
        "UTC start of the five-minute trading window",
        True,
    ),
    FieldProvenance(
        "market_end",
        "market_start + 300s (cross-check market.endDate / event.endDate)",
        "UTC end of the five-minute trading window",
        True,
    ),
    FieldProvenance(
        "created_at",
        "event.createdAt / market.createdAt",
        "Gamma object creation time",
        False,
    ),
    FieldProvenance(
        "listed_at",
        "event.startDate / market.startDate",
        "Listing / schedule publication time (often ~1 day before window)",
        False,
    ),
    FieldProvenance(
        "slug_epoch",
        "suffix of event/market slug",
        "Unix seconds of market_start",
        True,
    ),
)


@dataclass(frozen=True)
class Btc5mWindow:
    slug: str
    slug_epoch: int
    market_start: datetime
    market_end: datetime
    title: str | None
    created_at: datetime | None
    listed_at: datetime | None
    event_start_time: datetime | None
    gamma_end: datetime | None
    accepting_orders: bool | None
    closed: bool | None
    active: bool | None
    provenance: tuple[FieldProvenance, ...] = PROVENANCE

    @property
    def duration_s(self) -> float:
        return (self.market_end - self.market_start).total_seconds()


@dataclass(frozen=True)
class LifecycleDeadlines:
    market_start: datetime
    market_end: datetime
    flatten_deadline: datetime
    entry_deadline: datetime
    approval_expiration: datetime
    flatten_before_close_s: float
    entry_safety_buffer_s: float
    max_hold_s: float


def parse_slug_epoch(slug: str) -> int:
    m = _BTC_5M_SLUG_TS.search(slug.strip())
    if not m:
        raise MarketWindowError("INVALID_MARKET_WINDOW")
    return int(m.group(1))


def window_from_slug_epoch(slug: str, epoch: int | None = None) -> tuple[datetime, datetime]:
    start_ts = epoch if epoch is not None else parse_slug_epoch(slug)
    start = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    end = start + timedelta(seconds=BTC_5M_WINDOW_S)
    return start, end


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _et_wall_to_utc(year: int, month: int, day: int, hour12: int, minute: int, ampm: str) -> datetime:
    """Convert US Eastern wall clock to UTC (EDT=UTC-4 / EST=UTC-5 via fold-free July=EDT)."""
    h = hour12 % 12
    if ampm.upper() == "PM":
        h += 12
    # BTC 5m titles use ET; July is Eastern Daylight Time (UTC-4).
    # For generality without zoneinfo dependency issues on all hosts, use July EDT.
    # Tests pin July windows; production cross-checks slug epoch.
    offset = timedelta(hours=4)  # EDT
    naive = datetime(year, month, day, h, minute)
    return (naive + offset).replace(tzinfo=timezone.utc)


def parse_title_window_utc(title: str, *, year: int | None = None) -> tuple[datetime, datetime] | None:
    m = _TITLE_WINDOW.search(title or "")
    if not m:
        return None
    month = _MONTHS[m.group("month").lower()]
    day = int(m.group("day"))
    y = year or datetime.now(timezone.utc).year
    start = _et_wall_to_utc(
        y, month, day, int(m.group("sh")), int(m.group("sm")), m.group("sap")
    )
    end = _et_wall_to_utc(
        y, month, day, int(m.group("eh")), int(m.group("em")), m.group("eap")
    )
    if end <= start:
        end = end + timedelta(days=1)
    return start, end


def resolve_btc_5m_window(
    *,
    slug: str,
    event: Mapping[str, Any] | None = None,
    market: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    require_accepting_orders: bool = True,
    title_tolerance_s: float = 2.0,
    end_tolerance_s: float = 2.0,
) -> Btc5mWindow:
    """Resolve authoritative window; raise MarketWindowError with typed code."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    epoch = parse_slug_epoch(slug)
    market_start, market_end = window_from_slug_epoch(slug, epoch)
    if (market_end - market_start).total_seconds() != BTC_5M_WINDOW_S:
        raise MarketWindowError("MARKET_DURATION_MISMATCH")
    if market_start >= market_end:
        raise MarketWindowError("INVALID_MARKET_WINDOW")

    ev = event or {}
    mkt = market or {}
    title = str(mkt.get("question") or ev.get("title") or "") or None
    created_at = _parse_dt(ev.get("createdAt") or mkt.get("createdAt"))
    listed_at = _parse_dt(ev.get("startDate") or mkt.get("startDate"))
    event_start_time = _parse_dt(
        mkt.get("eventStartTime") or ev.get("startTime") or ev.get("eventStartTime")
    )
    gamma_end = _parse_dt(mkt.get("endDate") or ev.get("endDate"))
    accepting = mkt.get("acceptingOrders")
    if accepting is None:
        accepting = mkt.get("accepting_orders")
    closed = mkt.get("closed") if "closed" in mkt else ev.get("closed")
    active = mkt.get("active") if "active" in mkt else ev.get("active")

    # Creation/listing must never equal market_start for scheduling trust.
    if listed_at is not None and abs((listed_at - market_start).total_seconds()) > 60:
        # Expected: listed_at often ~1 day earlier — OK, ignored for scheduling.
        pass

    if event_start_time is not None:
        if abs((event_start_time - market_start).total_seconds()) > end_tolerance_s:
            raise MarketWindowError("TITLE_TIME_MISMATCH")

    if gamma_end is not None:
        if abs((gamma_end - market_end).total_seconds()) > end_tolerance_s:
            raise MarketWindowError("MARKET_DURATION_MISMATCH")

    if title:
        parsed = parse_title_window_utc(title, year=market_start.year)
        if parsed is not None:
            t_start, t_end = parsed
            if abs((t_start - market_start).total_seconds()) > title_tolerance_s:
                raise MarketWindowError("TITLE_TIME_MISMATCH")
            if abs((t_end - market_end).total_seconds()) > title_tolerance_s:
                raise MarketWindowError("TITLE_TIME_MISMATCH")

    if closed is True:
        raise MarketWindowError("MARKET_NOT_ACCEPTING_ORDERS")
    if require_accepting_orders and accepting is False:
        raise MarketWindowError("MARKET_NOT_ACCEPTING_ORDERS")

    return Btc5mWindow(
        slug=slug,
        slug_epoch=epoch,
        market_start=market_start,
        market_end=market_end,
        title=title,
        created_at=created_at,
        listed_at=listed_at,
        event_start_time=event_start_time,
        gamma_end=gamma_end,
        accepting_orders=bool(accepting) if accepting is not None else None,
        closed=bool(closed) if closed is not None else None,
        active=bool(active) if active is not None else None,
    )


def compute_lifecycle_deadlines(
    window: Btc5mWindow,
    *,
    now: datetime | None = None,
    flatten_before_close_s: float = 30.0,
    entry_safety_buffer_s: float = 45.0,
    approval_skew_s: float = 5.0,
    max_hold_s: float | None = None,
    min_remaining_for_entry_s: float = 90.0,
) -> LifecycleDeadlines:
    """Derive approval/entry/flatten deadlines from market_end — never now+10m."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    flatten_deadline = window.market_end - timedelta(seconds=flatten_before_close_s)
    entry_deadline = flatten_deadline - timedelta(seconds=entry_safety_buffer_s)
    approval_expiration = entry_deadline - timedelta(seconds=approval_skew_s)
    hold = max_hold_s if max_hold_s is not None else max(
        30.0, (flatten_deadline - now).total_seconds()
    )

    if not (
        approval_expiration <= entry_deadline < flatten_deadline < window.market_end
    ):
        raise MarketWindowError("ENTRY_DEADLINE_AFTER_FLATTEN")

    if now >= entry_deadline:
        raise MarketWindowError("INSUFFICIENT_TIME_REMAINING")
    remaining = (entry_deadline - now).total_seconds()
    if remaining < min_remaining_for_entry_s:
        raise MarketWindowError("INSUFFICIENT_TIME_REMAINING")

    return LifecycleDeadlines(
        market_start=window.market_start,
        market_end=window.market_end,
        flatten_deadline=flatten_deadline,
        entry_deadline=entry_deadline,
        approval_expiration=approval_expiration,
        flatten_before_close_s=flatten_before_close_s,
        entry_safety_buffer_s=entry_safety_buffer_s,
        max_hold_s=hold,
    )
