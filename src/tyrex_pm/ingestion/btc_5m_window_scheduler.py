"""BTC Up/Down 5m window scheduling for run_continue (trading, not recording)."""

from __future__ import annotations

from dataclasses import dataclass

from tyrex_pm.ingestion.market_discovery import (
    BTC_5M_SLUG_PREFIX,
    BTC_5M_WINDOW_S,
    is_btc_5m_event_slug,
)
from tyrex_pm.venue.polymarket.event_metadata import EventMetadataError, parse_event_ref

_EVENT_URL_PREFIX = "https://polymarket.com/event/"


@dataclass(frozen=True)
class Btc5mWindowPlan:
    window_start_ts: int
    window_end_ts: int
    event_slug: str
    event_url: str
    wake_at_ts: float
    skip_reason: str | None = None


def validate_btc_5m_reference_url(reference_url: str) -> str:
    """Validate reference URL/slug as BTC 5m family; return event slug."""
    ref = parse_event_ref(reference_url)
    slug = ref.slug.strip()
    if not is_btc_5m_event_slug(slug):
        raise EventMetadataError(
            f"reference event is not a BTC Up/Down 5m slug (got {slug!r}); "
            f"expected prefix {BTC_5M_SLUG_PREFIX}<unix_start_ts>"
        )
    return slug


def current_btc_5m_window_start(now_ts: float) -> int:
    """Return unix start timestamp of the 5m window containing *now_ts*."""
    now = int(now_ts)
    return now - (now % BTC_5M_WINDOW_S)


def btc_5m_event_slug(window_start_ts: int) -> str:
    return f"{BTC_5M_SLUG_PREFIX}{int(window_start_ts)}"


def btc_5m_canonical_event_url(window_start_ts: int) -> str:
    return f"{_EVENT_URL_PREFIX}{btc_5m_event_slug(window_start_ts)}"


def select_next_btc_5m_trading_window(
    *,
    now_ts: float,
    prestart_seconds: float = 30.0,
    entry_grace_seconds: float = 15.0,
) -> Btc5mWindowPlan:
    """Select the next tradable BTC 5m window and when to start the one-shot run."""
    aligned = current_btc_5m_window_start(now_ts)
    candidate_start = aligned + BTC_5M_WINDOW_S

    skip_reason: str | None = None
    while now_ts >= candidate_start + entry_grace_seconds:
        skip_reason = "past_entry_grace"
        candidate_start += BTC_5M_WINDOW_S

    wake_at = max(now_ts, float(candidate_start) - float(prestart_seconds))
    slug = btc_5m_event_slug(candidate_start)
    return Btc5mWindowPlan(
        window_start_ts=candidate_start,
        window_end_ts=candidate_start + BTC_5M_WINDOW_S,
        event_slug=slug,
        event_url=btc_5m_canonical_event_url(candidate_start),
        wake_at_ts=wake_at,
        skip_reason=skip_reason,
    )
