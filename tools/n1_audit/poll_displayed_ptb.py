#!/usr/bin/env python3
"""Poll Polymarket event pages for SSR dehydrated openPrice (read-only).

Extracts structured openPrice/closePrice from page payload — not HTML label scraping.
Appends JSONL records compatible with capture_sources schema.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

UA = "TyrexPM-N1-Audit/1.0 (read-only research)"
WINDOW_S = 300


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_html(slug: str) -> str:
    url = f"https://polymarket.com/event/{slug}"
    req = Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    with urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def extract_open_close(html: str, start_iso: str) -> dict | None:
    """Find dehydrated crypto-prices query for the window start.

    Polymarket SSR embeds React Query state with JSON string-escaped quotes
    (``\\"openPrice\\"``), not raw ``"openPrice"``.
    """
    # Prefer the query keyed by start time. Allow both raw and escaped quotes.
    q = re.escape(start_iso)
    pats = [
        re.compile(
            r'\\?"data\\?":\s*\{\\?"openPrice\\?":([0-9.eE+-]+),\\?"closePrice\\?":'
            r'(null|[0-9.eE+-]+)\}'
            r'.*?\\?"queryKey\\?":\s*\[\\?"crypto-prices\\?",\\?"price\\?",\\?"BTC\\?",\\?"'
            + q
            + r'\\?"',
            re.DOTALL,
        ),
        # queryKey may appear before data in some payloads
        re.compile(
            r'\\?"queryKey\\?":\s*\[\\?"crypto-prices\\?",\\?"price\\?",\\?"BTC\\?",\\?"'
            + q
            + r'\\?".*?\\?"data\\?":\s*\{\\?"openPrice\\?":([0-9.eE+-]+),\\?"closePrice\\?":'
            r'(null|[0-9.eE+-]+)\}',
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
    m2 = re.search(
        r'\\?"openPrice\\?":([0-9.eE+-]+),\\?"closePrice\\?":(null|[0-9.eE+-]+)',
        html,
    )
    if not m2:
        return None
    return {
        "openPrice": float(m2.group(1)),
        "closePrice": None if m2.group(2) == "null" else float(m2.group(2)),
        "matched_start": False,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-s", type=float, default=1100.0)
    ap.add_argument("--interval-s", type=float, default=5.0)
    ap.add_argument("--out", type=Path, default=Path("var/recordings/n1/displayed_ptb.jsonl"))
    args = ap.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    mono0 = time.perf_counter_ns()
    seq = 0
    end = time.time() + args.duration_s
    last: dict[str, float | None] = {}
    with args.out.open("a", encoding="utf-8") as fp:
        while time.time() < end:
            now = time.time()
            epochs = [(int(now) // WINDOW_S) * WINDOW_S + off for off in (-WINDOW_S, 0, WINDOW_S)]
            for epoch in epochs:
                slug = f"btc-updown-5m-{epoch}"
                start_iso = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                try:
                    html = get_html(slug)
                    data = extract_open_close(html, start_iso)
                    if not data:
                        continue
                    # Do not record unmatched openPrice fallbacks (wrong-window risk).
                    if not data.get("matched_start"):
                        continue
                    key = f"{slug}:{data['openPrice']}:{data['closePrice']}"
                    if last.get(slug) == data["openPrice"] and data.get("closePrice") == last.get(
                        slug + ":close"
                    ):
                        continue
                    last[slug] = data["openPrice"]
                    last[slug + ":close"] = data.get("closePrice")
                    seq += 1
                    rec = {
                        "capture_sequence": seq,
                        "source": "displayed_ptb_ssr_openPrice",
                        "symbol": "BTC",
                        "window_id": slug,
                        "source_ts": None,
                        "source_ts_ms": None,
                        "receive_wall_utc": iso_now(),
                        "receive_monotonic_ns": time.perf_counter_ns() - mono0,
                        "clock_uncertainty_ms": None,
                        "value": data["openPrice"],
                        "provider_sequence_id": None,
                        "connection_id": "http-ssr",
                        "late_or_out_of_order": None,
                        "raw_event_fingerprint": hashlib.sha256(key.encode()).hexdigest()[:16],
                        "extra": {
                            "closePrice": data.get("closePrice"),
                            "matched_start": data.get("matched_start"),
                            "query_start": start_iso,
                            "provenance": "SSR dehydrated React Query state queryKey=["
                            "crypto-prices,price,BTC,start,fiveminute,end]",
                        },
                    }
                    fp.write(json.dumps(rec, separators=(",", ":")) + "\n")
                    fp.flush()
                    print("PTB", slug, data["openPrice"], "close", data.get("closePrice"))
                except Exception as e:
                    print("ERR", slug, type(e).__name__, e)
            time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
