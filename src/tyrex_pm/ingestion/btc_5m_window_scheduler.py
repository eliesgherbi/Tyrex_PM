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


@dataclass(frozen=True)
class Btc5mSkippedWindow:
    window_start_ts: int
    window_end_ts: int
    event_slug: str
    event_url: str
    reason: str
    lead_time_s: float


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


def _next_window_start_after(now_ts: float) -> int:
    aligned = current_btc_5m_window_start(now_ts)
    candidate = aligned + BTC_5M_WINDOW_S
    if now_ts >= candidate:
        candidate += BTC_5M_WINDOW_S
    return candidate


def select_btc_5m_session_window(
    *,
    now_ts: float,
    target_prestart_seconds: float = 90.0,
    hard_min_prestart_seconds: float = 20.0,
    min_sigma_warmup_seconds: float = 0.0,
    max_scan_windows: int = 48,
) -> tuple[Btc5mWindowPlan, tuple[Btc5mSkippedWindow, ...]]:
    """Select the next BTC 5m window with sufficient pre-boundary lead time.

    Skips windows with lead time below ``target_prestart_seconds`` (preferred),
    below ``min_sigma_warmup_seconds`` (sigma warm-up requirement), or below
    ``hard_min_prestart_seconds`` (absolute minimum).
    """
    skipped: list[Btc5mSkippedWindow] = []
    candidate_start = _next_window_start_after(now_ts)
    effective_target = max(target_prestart_seconds, min_sigma_warmup_seconds)

    for _ in range(max_scan_windows):
        lead = float(candidate_start) - float(now_ts)
        slug = btc_5m_event_slug(candidate_start)
        url = btc_5m_canonical_event_url(candidate_start)
        end_ts = candidate_start + BTC_5M_WINDOW_S

        if lead < hard_min_prestart_seconds:
            skipped.append(
                Btc5mSkippedWindow(
                    window_start_ts=candidate_start,
                    window_end_ts=end_ts,
                    event_slug=slug,
                    event_url=url,
                    reason="insufficient_prestart_hard_min",
                    lead_time_s=lead,
                )
            )
            candidate_start += BTC_5M_WINDOW_S
            continue

        if lead < min_sigma_warmup_seconds:
            skipped.append(
                Btc5mSkippedWindow(
                    window_start_ts=candidate_start,
                    window_end_ts=end_ts,
                    event_slug=slug,
                    event_url=url,
                    reason="insufficient_sigma_warmup_lead",
                    lead_time_s=lead,
                )
            )
            candidate_start += BTC_5M_WINDOW_S
            continue

        if lead < effective_target:
            reason = (
                "insufficient_sigma_warmup_lead"
                if effective_target == min_sigma_warmup_seconds > target_prestart_seconds
                else "insufficient_prestart_target"
            )
            skipped.append(
                Btc5mSkippedWindow(
                    window_start_ts=candidate_start,
                    window_end_ts=end_ts,
                    event_slug=slug,
                    event_url=url,
                    reason=reason,
                    lead_time_s=lead,
                )
            )
            candidate_start += BTC_5M_WINDOW_S
            continue

        wake_at = max(now_ts, float(candidate_start) - effective_target)
        return (
            Btc5mWindowPlan(
                window_start_ts=candidate_start,
                window_end_ts=end_ts,
                event_slug=slug,
                event_url=url,
                wake_at_ts=wake_at,
                skip_reason=None,
            ),
            tuple(skipped),
        )

    raise RuntimeError("no eligible BTC 5m window found within scan horizon")


def prestart_lead_seconds(*, now_ts: float, event_start_ts: float) -> float:
    return float(event_start_ts) - float(now_ts)


def prestart_sufficient(
    *,
    now_ts: float,
    event_start_ts: float,
    hard_min_prestart_seconds: float = 20.0,
) -> bool:
    return prestart_lead_seconds(now_ts=now_ts, event_start_ts=event_start_ts) >= hard_min_prestart_seconds
