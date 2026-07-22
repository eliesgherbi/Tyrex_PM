"""Live SSR displayed-PTB attestation adapter (read-only, public HTTP).

Fetches Polymarket event-page dehydrated ``openPrice`` for a BTC 5m window
and exposes it via ``PtbAttestationPort``. Displayed PTB is **comparison
evidence only** — never substituted as independently derived Chainlink K.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping
from urllib.request import Request, urlopen

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import MarketId
from tyrex_pm.core.numerics import as_decimal

UA = "TyrexPM-N3B-Attestation/1.0 (read-only)"


class DisabledSsrAttestationProvider:
    """No-op SSR port used when ``require_ssr_price_match`` is false.

    Fetch must never be invoked in the disabled path; calling it is a wiring bug.
    """

    fetch_count: int = 0

    def fetch_attestation(
        self,
        *,
        market_id: MarketId,
        window_id: str,
        event_start: datetime,
    ) -> tuple[Decimal | None, str, Mapping[str, Any], datetime | None]:
        _ = market_id, window_id, event_start
        type(self).fetch_count += 1
        raise RuntimeError(
            "SSR attestation disabled (require_ssr_price_match=false); "
            "fetch_attestation must not be called"
        )


def fetch_event_html(slug: str, *, timeout_s: float = 30.0) -> str:
    url = f"https://polymarket.com/event/{slug}"
    req = Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    with urlopen(req, timeout=timeout_s) as resp:
        return resp.read().decode("utf-8", "replace")


def extract_open_close(html: str, start_iso: str) -> dict[str, Any] | None:
    """Extract SSR crypto-prices open/close for the window start queryKey."""
    q = re.escape(start_iso)
    pats = [
        re.compile(
            r'\\?"data\\?":\s*\{\\?"openPrice\\?":([0-9.eE+-]+),\\?"closePrice\\?":'
            r"(null|[0-9.eE+-]+)\}"
            r'.*?\\?"queryKey\\?":\s*\[\\?"crypto-prices\\?",\\?"price\\?",\\?"BTC\\?",\\?"'
            + q
            + r'\\?"',
            re.DOTALL,
        ),
        re.compile(
            r'\\?"queryKey\\?":\s*\[\\?"crypto-prices\\?",\\?"price\\?",\\?"BTC\\?",\\?"'
            + q
            + r'\\?".*?\\?"data\\?":\s*\{\\?"openPrice\\?":([0-9.eE+-]+),\\?"closePrice\\?":'
            r"(null|[0-9.eE+-]+)\}",
            re.DOTALL,
        ),
    ]
    for pat in pats:
        m = pat.search(html)
        if m:
            return {
                "openPrice": float(m.group(1)),
                "closePrice": None if m.group(2) == "null" else float(m.group(2)),
                "matched_start": True,
                "query_start": start_iso,
            }
    return None


class SsrDisplayedPtbAttestationProvider:
    """Public SSR openPrice attestation — independent of Chainlink candidate K."""

    def __init__(
        self,
        *,
        timeout_s: float = 30.0,
        retries: int = 4,
        retry_delay_s: float = 2.0,
    ) -> None:
        self._timeout_s = timeout_s
        self._retries = max(1, retries)
        self._retry_delay_s = max(0.0, retry_delay_s)
        self._cache: dict[str, tuple[Decimal, str, dict[str, Any], datetime]] = {}
        self._neg_cache_until: dict[str, datetime] = {}

    def fetch_attestation(
        self,
        *,
        market_id: MarketId,
        window_id: str,
        event_start: datetime,
    ) -> tuple[Decimal | None, str, Mapping[str, Any], datetime | None]:
        import time

        _ = market_id
        event_start = require_utc(event_start, field_name="event_start")
        cached = self._cache.get(window_id)
        if cached is not None:
            return cached[0], cached[1], dict(cached[2]), cached[3]

        now = datetime.now(timezone.utc)
        neg_until = self._neg_cache_until.get(window_id)
        if neg_until is not None and now < neg_until:
            return (
                None,
                "ssr_open_price_unmatched",
                {"window_id": window_id, "neg_cached": True},
                now,
            )

        slug = window_id if window_id.startswith("btc-updown-5m-") else f"btc-updown-5m-{window_id}"
        start_iso = event_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start_iso_alt = event_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        last_err: dict[str, Any] = {}
        # Single-attempt fetch by default in the hot path; caller may retry across ticks.
        # When retries>1, allow a short bounded burst (used by one-shot CLIs).
        attempts = self._retries
        for attempt in range(attempts):
            try:
                html = fetch_event_html(slug, timeout_s=self._timeout_s)
            except Exception as exc:
                last_err = {
                    "window_id": window_id,
                    "error": f"{type(exc).__name__}:{exc}",
                    "attempt": attempt + 1,
                }
                if attempt + 1 < attempts:
                    time.sleep(self._retry_delay_s)
                continue
            extracted = extract_open_close(html, start_iso) or extract_open_close(
                html, start_iso_alt
            )
            available_at = datetime.now(timezone.utc)
            if extracted is not None and extracted.get("matched_start"):
                # JSON numbers arrive as float; convert via str to avoid binary float ingress.
                value = as_decimal(str(extracted["openPrice"]), field_name="attested_value")
                prov = {
                    "window_id": window_id,
                    "slug": slug,
                    "query_start": start_iso,
                    "matched_start": True,
                    "closePrice": extracted.get("closePrice"),
                    "role": "independent_displayed_comparison_only",
                    "not_substituted_as_k": True,
                    "attempt": attempt + 1,
                }
                self._cache[window_id] = (
                    value,
                    "polymarket_ssr_openPrice",
                    prov,
                    available_at,
                )
                self._neg_cache_until.pop(window_id, None)
                return value, "polymarket_ssr_openPrice", prov, available_at
            last_err = {
                "window_id": window_id,
                "slug": slug,
                "query_start": start_iso,
                "matched_start": False,
                "attempt": attempt + 1,
            }
            if attempt + 1 < attempts:
                time.sleep(self._retry_delay_s)
        # Brief negative cache so feed loops do not stampede SSR.
        self._neg_cache_until[window_id] = datetime.now(timezone.utc) + timedelta(
            seconds=max(1.0, self._retry_delay_s)
        )
        return None, "ssr_open_price_unmatched", last_err, datetime.now(timezone.utc)
