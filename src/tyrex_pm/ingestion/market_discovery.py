"""Low-cadence BTC 5m market discovery for record-only mode (M2B.1-B)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from tyrex_pm.venue.polymarket.event_metadata import (
    GAMMA_BASE,
    EventMetadataLookupError,
    _BTC_5M_SLUG_TS_RE,
    _fetch_event_by_slug,
    _metadata_from_event_market,
    _select_market,
    btc_5m_market_id_from_end_ts,
)

log = logging.getLogger(__name__)

BTC_5M_WINDOW_S = 300
BTC_5M_SLUG_PREFIX = "btc-updown-5m-"


@dataclass(frozen=True)
class MarketDiscoveryResult:
    market_id: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    event_start_ts: float
    event_end_ts: float
    event_slug: str
    event_title: str = ""
    yes_outcome_label: str = ""
    no_outcome_label: str = ""
    market_slug: str = ""


def is_btc_5m_event_slug(slug: str) -> bool:
    return bool(_BTC_5M_SLUG_TS_RE.search(str(slug).strip()))


def btc_5m_window_start_timestamps(
    now_ts: float | None = None,
    *,
    lookback_windows: int = 1,
    lookahead_windows: int = 2,
) -> list[int]:
    """Return unix start timestamps for BTC 5m windows near *now_ts*."""
    now = int(now_ts if now_ts is not None else time.time())
    aligned = now - (now % BTC_5M_WINDOW_S)
    starts: list[int] = []
    for offset in range(-lookback_windows, lookahead_windows + 1):
        starts.append(aligned + offset * BTC_5M_WINDOW_S)
    return starts


def parse_btc_5m_discovery_from_event(event: dict[str, Any]) -> MarketDiscoveryResult | None:
    """Parse a Gamma event payload into a discovery result, or None if not BTC 5m."""
    slug = str(event.get("slug", "")).strip()
    if not is_btc_5m_event_slug(slug):
        return None
    try:
        market = _select_market(event, market_slug=None)
        meta = _metadata_from_event_market(event, market)
    except Exception:
        return None
    return market_discovery_result_from_metadata(meta)


def market_discovery_result_from_metadata(meta) -> MarketDiscoveryResult:
    return MarketDiscoveryResult(
        market_id=meta.market_id,
        condition_id=meta.condition_id,
        yes_token_id=meta.yes_token_id,
        no_token_id=meta.no_token_id,
        event_start_ts=meta.event_start_ts,
        event_end_ts=meta.event_end_ts,
        event_slug=meta.event_slug,
        event_title=meta.event_title,
        yes_outcome_label=meta.yes_outcome_label,
        no_outcome_label=meta.no_outcome_label,
        market_slug=meta.market_slug,
    )


def fetch_btc_5m_event_by_slug(
    slug: str,
    *,
    gamma_base: str = GAMMA_BASE,
    timeout_s: float = 15.0,
) -> dict[str, Any]:
    with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
        return _fetch_event_by_slug(client, gamma_base=gamma_base, slug=slug)


def discover_btc_5m_by_slug(
    slug: str,
    *,
    gamma_base: str = GAMMA_BASE,
    timeout_s: float = 15.0,
) -> MarketDiscoveryResult | None:
    if not is_btc_5m_event_slug(slug):
        return None
    try:
        event = fetch_btc_5m_event_by_slug(slug, gamma_base=gamma_base, timeout_s=timeout_s)
    except EventMetadataLookupError:
        return None
    return parse_btc_5m_discovery_from_event(event)


def discover_btc_5m_near_now(
    *,
    now_ts: float | None = None,
    lookback_windows: int = 1,
    lookahead_windows: int = 2,
    gamma_base: str = GAMMA_BASE,
    timeout_s: float = 15.0,
) -> list[MarketDiscoveryResult]:
    """Synchronous one-shot discovery for tests and coverage fixtures."""
    seen: set[str] = set()
    results: list[MarketDiscoveryResult] = []
    for start_ts in btc_5m_window_start_timestamps(
        now_ts,
        lookback_windows=lookback_windows,
        lookahead_windows=lookahead_windows,
    ):
        slug = f"{BTC_5M_SLUG_PREFIX}{start_ts}"
        if slug in seen:
            continue
        discovered = discover_btc_5m_by_slug(slug, gamma_base=gamma_base, timeout_s=timeout_s)
        if discovered is None or discovered.market_id in seen:
            continue
        seen.add(slug)
        seen.add(discovered.market_id)
        results.append(discovered)
    return results


ELIGIBLE = "eligible"
TOO_EARLY = "too_early"
EXPIRED = "expired"


def classify_market_recording_eligibility(
    result: MarketDiscoveryResult,
    *,
    now_ts: float | None = None,
    post_close_grace_s: float = 60.0,
    pre_open_recording_lead_s: float = 60.0,
    skip_expired_markets: bool = True,
) -> str:
    """Return whether a discovered market should start recording now.

    - ``eligible``: start recording (active window, within pre-open lead, or in grace)
    - ``too_early``: future market beyond ``pre_open_recording_lead_s``; retry later
    - ``expired``: window close + grace already passed; skip permanently
    """
    now = time.time() if now_ts is None else now_ts
    if skip_expired_markets and now >= result.event_end_ts + post_close_grace_s:
        return EXPIRED
    if now < result.event_start_ts - pre_open_recording_lead_s:
        return TOO_EARLY
    return ELIGIBLE


async def run_market_discovery(
    *,
    stop: asyncio.Event,
    on_market_discovered: Callable[[MarketDiscoveryResult], Any],
    poll_interval_s: float = 25.0,
    gamma_base: str = GAMMA_BASE,
    lookback_windows: int = 1,
    lookahead_windows: int = 2,
    post_close_grace_s: float = 60.0,
    pre_open_recording_lead_s: float = 60.0,
    skip_expired_markets: bool = True,
    on_market_skipped: Callable[[MarketDiscoveryResult, str], Any] | None = None,
) -> None:
    """Poll Gamma for active/upcoming BTC 5m markets; dedupe and emit discoveries."""
    seen_slugs: set[str] = set()
    seen_market_ids: set[str] = set()

    async def _emit(result: MarketDiscoveryResult) -> None:
        maybe = on_market_discovered(result)
        if asyncio.iscoroutine(maybe):
            await maybe

    async def _skip(result: MarketDiscoveryResult, reason: str) -> None:
        if on_market_skipped is None:
            return
        maybe = on_market_skipped(result, reason)
        if asyncio.iscoroutine(maybe):
            await maybe

    while not stop.is_set():
        for start_ts in btc_5m_window_start_timestamps(
            lookback_windows=lookback_windows,
            lookahead_windows=lookahead_windows,
        ):
            if stop.is_set():
                break
            slug = f"{BTC_5M_SLUG_PREFIX}{start_ts}"
            if slug in seen_slugs:
                continue
            try:
                event = await asyncio.to_thread(fetch_btc_5m_event_by_slug, slug, gamma_base=gamma_base)
            except EventMetadataLookupError:
                continue
            except Exception as exc:
                log.warning("market_discovery: fetch %s failed: %r", slug, exc)
                continue

            result = parse_btc_5m_discovery_from_event(event)
            if result is None:
                continue
            if result.market_id in seen_market_ids:
                seen_slugs.add(slug)
                continue

            eligibility = classify_market_recording_eligibility(
                result,
                post_close_grace_s=post_close_grace_s,
                pre_open_recording_lead_s=pre_open_recording_lead_s,
                skip_expired_markets=skip_expired_markets,
            )
            if eligibility == TOO_EARLY:
                log.debug(
                    "market_discovery: defer market_id=%s until pre_open lead window",
                    result.market_id,
                )
                continue
            if eligibility == EXPIRED:
                seen_slugs.add(slug)
                seen_market_ids.add(result.market_id)
                log.info(
                    "market_discovery: skip expired market_id=%s slug=%s",
                    result.market_id,
                    result.event_slug,
                )
                await _skip(result, EXPIRED)
                continue

            seen_slugs.add(slug)
            seen_market_ids.add(result.market_id)
            log.info(
                "market_discovered market_id=%s slug=%s ends=%s",
                result.market_id,
                result.event_slug,
                result.event_end_ts,
            )
            await _emit(result)

        try:
            await asyncio.wait_for(stop.wait(), timeout=max(1.0, poll_interval_s))
        except asyncio.TimeoutError:
            continue
