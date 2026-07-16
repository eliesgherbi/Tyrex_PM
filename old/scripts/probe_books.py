"""Compare production vs historical transition /book responses for token_ids.

Post-cutover production V2 lives at ``https://clob.polymarket.com``. The old
``clob-v2.polymarket.com`` host is kept here only as a diagnostic for stale DNS
or redirect issues; runtime code does not use it as the default.
"""

from __future__ import annotations

import json
from typing import Any

import httpx


HOSTS = {
    "V2 production (clob.polymarket.com)": "https://clob.polymarket.com/book",
    "historical transition (clob-v2.polymarket.com)": "https://clob-v2.polymarket.com/book",
}

TOKENS = {
    "BTC_Up_Apr21_YES": "62972170142558033652560290765558335835622641212916058921517773100969409379935",
    "BTC_Up_Apr21_NO": "33079713415390964319492771039491404155897285990436441393527598475952915642677",
    "BTC_150k_YES": "13915689317269078219168496739008737517740566192006337297676041270492637394586",
    "Iran_peace_YES": "10355316169421062771540371697837923442956106006258739802114788264214901200573",
}


def _alive(levels: Any) -> int:
    if not levels or not isinstance(levels, list):
        return 0
    return sum(1 for lv in levels if isinstance(lv, dict) and float(lv.get("size", "0")) > 0)


def _best(levels: Any, side: str) -> str:
    if not levels:
        return "-"
    prices = [float(lv["price"]) for lv in levels if float(lv.get("size", "0")) > 0]
    if not prices:
        return "-"
    return f"{(max(prices) if side == 'bid' else min(prices)):.4f}"


for tname, tid in TOKENS.items():
    print(f"\n=== {tname} ===")
    for hname, url in HOSTS.items():
        try:
            r = httpx.get(url, params={"token_id": tid}, timeout=15)
            r.raise_for_status()
            book = r.json()
        except Exception as e:
            print(f"  {hname:32s}  ERROR {e!r}")
            continue
        bb = _best(book.get("bids"), "bid")
        ba = _best(book.get("asks"), "ask")
        nb = _alive(book.get("bids"))
        na = _alive(book.get("asks"))
        print(
            f"  {hname:32s}  best_bid={bb:>6}  best_ask={ba:>6}  "
            f"levels={nb}b/{na}a  tick={book.get('tick_size')}"
        )
