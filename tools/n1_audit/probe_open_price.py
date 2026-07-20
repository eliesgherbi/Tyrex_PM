#!/usr/bin/env python3
"""Probe Polymarket openPrice/PTB structured sources (read-only)."""

from __future__ import annotations

import json
import re
import urllib.request

UA = "TyrexPM-N1-Audit/1.0 (read-only research)"
SLUG = "btc-updown-5m-1784581800"


def get(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read()


def main() -> None:
    status, html_b = get(f"https://polymarket.com/event/{SLUG}")
    html = html_b.decode("utf-8", "replace")
    print("page_status", status, "len", len(html))
    idx = html.find("openPrice")
    print("openPrice_idx", idx)
    if idx >= 0:
        print("CTX", html[idx - 400 : idx + 500])

    # Extract queryKey-ish strings
    for m in re.finditer(r"crypto-prices.{0,200}", html):
        print("HIT", m.group(0)[:220])
        if m.start() > 5:
            break

    # Try common BFF patterns used by Next apps
    epoch = 1784581800
    start = "2026-07-20T21:10:00.000Z"
    end = "2026-07-20T21:15:00.000Z"
    candidates = [
        f"https://polymarket.com/api/crypto/price?symbol=btc&eventSlug={SLUG}",
        f"https://polymarket.com/api/crypto/price?slug={SLUG}",
        f"https://polymarket.com/api/crypto/price?variant=btc-up-or-down-5m&start={start}&end={end}",
        f"https://polymarket.com/api/crypto/price?series=btc-up-or-down-5m&startTime={start}",
        f"https://polymarket.com/api/crypto/price?asset=BTC&startTime={epoch}&endTime={epoch+300}",
        "https://polymarket.com/api/crypto/price?symbol=btc/usd&startTime=2026-07-20T21:10:00Z",
        f"https://polymarket.com/api/series/btc-up-or-down-5m/price?slug={SLUG}",
        f"https://polymarket.com/api/event/{SLUG}/price",
        f"https://polymarket.com/api/events/{SLUG}/crypto-price",
    ]
    for url in candidates:
        try:
            st, body = get(url)
            print("OK", st, url, body[:240])
        except Exception as e:
            print("FAIL", url, e)


if __name__ == "__main__":
    main()
