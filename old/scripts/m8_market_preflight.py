#!/usr/bin/env python3
"""Find a balanced Yes/No market for M8 WS-primary validation (Group E2 Step 1).

Scans Polymarket Gamma + CLOB /book. Does not change strategy parameters.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[1]
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

POSITION_SIZE = Decimal("5")
MIN_NOTIONAL_USD = Decimal("1")
MIN_ASK = Decimal("0.35")
MAX_ASK = Decimal("0.65")
MAX_SPREAD = Decimal("0.06")
MIN_DEPTH = Decimal("5")


@dataclass
class LegBook:
    token_id: str
    outcome: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    ask_depth: Decimal
    bid_depth: Decimal
    notional_at_size: Decimal | None


@dataclass
class MarketCandidate:
    market_id: str
    market_question: str
    market_slug: str
    event_title: str
    event_slug: str
    yes: LegBook
    no: LegBook
    combined_ask: Decimal | None
    max_spread: Decimal | None


def _loads_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        n = Decimal(str(value))
    except Exception:
        return None
    return n if n > 0 else None


def _book_leg(token_id: str, outcome: str, book: dict[str, Any]) -> LegBook:
    bids: list[tuple[Decimal, Decimal]] = []
    asks: list[tuple[Decimal, Decimal]] = []
    for side, out in (("bids", bids), ("asks", asks)):
        levels = book.get(side)
        if not isinstance(levels, list):
            continue
        for lv in levels:
            if not isinstance(lv, dict):
                continue
            price = _positive_decimal(lv.get("price"))
            size = _positive_decimal(lv.get("size"))
            if price is not None and size is not None:
                out.append((price, size))
    best_bid = max((p for p, _ in bids), default=None)
    best_ask = min((p for p, _ in asks), default=None)
    ask_depth = sum((s for p, s in asks if best_ask is not None and p == best_ask), Decimal("0"))
    bid_depth = sum((s for p, s in bids if best_bid is not None and p == best_bid), Decimal("0"))
    notional = POSITION_SIZE * best_ask if best_ask is not None else None
    return LegBook(
        token_id=token_id,
        outcome=outcome,
        best_bid=best_bid,
        best_ask=best_ask,
        ask_depth=ask_depth,
        bid_depth=bid_depth,
        notional_at_size=notional,
    )


def _market_tradeable(market: dict[str, Any]) -> bool:
    return (
        market.get("active") is True
        and market.get("closed") is not True
        and market.get("archived") is not True
        and market.get("acceptingOrders") is True
        and market.get("enableOrderBook") is True
    )


def _iter_binary_pairs(market: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    token_ids = _loads_jsonish(market.get("clobTokenIds"))
    outcomes = _loads_jsonish(market.get("outcomes"))
    if not isinstance(token_ids, list) or not isinstance(outcomes, list) or len(token_ids) != 2:
        return []
    pairs: list[tuple[str, str, str, str]] = []
    by_label: dict[str, str] = {}
    for tid, label in zip(token_ids, outcomes, strict=False):
        by_label[str(label).strip().lower()] = str(tid).strip()
    yes_id = by_label.get("yes")
    no_id = by_label.get("no")
    if yes_id and no_id:
        pairs.append((yes_id, no_id, "Yes", "No"))
    elif len(token_ids) == 2:
        pairs.append((str(token_ids[0]).strip(), str(token_ids[1]).strip(), str(outcomes[0]), str(outcomes[1])))
    return pairs


def _score(candidate: MarketCandidate) -> tuple:
    yes_ask = candidate.yes.best_ask or Decimal("9")
    no_ask = candidate.no.best_ask or Decimal("9")
    balance_penalty = abs(yes_ask - Decimal("0.5")) + abs(no_ask - Decimal("0.5"))
    spread = candidate.max_spread or Decimal("9")
    depth_penalty = Decimal("0")
    if candidate.yes.ask_depth < MIN_DEPTH:
        depth_penalty += MIN_DEPTH - candidate.yes.ask_depth
    if candidate.no.ask_depth < MIN_DEPTH:
        depth_penalty += MIN_DEPTH - candidate.no.ask_depth
    return (balance_penalty, spread, depth_penalty)


def scan_markets(
    *,
    max_events: int = 400,
    page_size: int = 100,
    timeout_s: float = 15.0,
    min_ask: Decimal = MIN_ASK,
    max_ask: Decimal = MAX_ASK,
) -> list[MarketCandidate]:
    candidates: list[MarketCandidate] = []
    headers = {"User-Agent": "TyrexPM-M8-Preflight/1.0"}

    with httpx.Client(timeout=timeout_s, follow_redirects=True, headers=headers) as client:
        for offset in range(0, max_events, page_size):
            resp = client.get(
                f"{GAMMA_BASE}/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": min(page_size, max_events - offset),
                    "offset": offset,
                },
            )
            resp.raise_for_status()
            events = resp.json()
            if not isinstance(events, list) or not events:
                break

            for event in events:
                if not isinstance(event, dict):
                    continue
                markets = event.get("markets")
                if not isinstance(markets, list):
                    continue
                for market in markets:
                    if not isinstance(market, dict) or not _market_tradeable(market):
                        continue
                    for yes_id, no_id, yes_label, no_label in _iter_binary_pairs(market):
                        books: dict[str, LegBook] = {}
                        ok = True
                        for tid, label in ((yes_id, yes_label), (no_id, no_label)):
                            book_resp = client.get(f"{CLOB_BASE}/book", params={"token_id": tid})
                            if book_resp.status_code != 200:
                                ok = False
                                break
                            books[label] = _book_leg(tid, label, book_resp.json())
                        if not ok:
                            continue
                        yes = books.get(yes_label) or books.get("Yes")
                        no = books.get(no_label) or books.get("No")
                        if yes is None or no is None:
                            continue
                        if yes.best_ask is None or no.best_ask is None:
                            continue
                        if yes.best_bid is None or no.best_bid is None:
                            continue
                        yes_spread = yes.best_ask - yes.best_bid
                        no_spread = no.best_ask - no.best_bid
                        if yes_spread > MAX_SPREAD or no_spread > MAX_SPREAD:
                            continue
                        if yes.best_ask < min_ask or yes.best_ask > max_ask:
                            continue
                        if no.best_ask < min_ask or no.best_ask > max_ask:
                            continue
                        if (yes.notional_at_size or Decimal("0")) < MIN_NOTIONAL_USD:
                            continue
                        if (no.notional_at_size or Decimal("0")) < MIN_NOTIONAL_USD:
                            continue
                        if yes.ask_depth < MIN_DEPTH or no.ask_depth < MIN_DEPTH:
                            continue
                        market_id = str(market.get("conditionId") or market.get("slug") or market.get("id") or "unknown")
                        candidates.append(
                            MarketCandidate(
                                market_id=market_id,
                                market_question=str(market.get("question", "")),
                                market_slug=str(market.get("slug", "")),
                                event_title=str(event.get("title", "")),
                                event_slug=str(event.get("slug", "")),
                                yes=yes,
                                no=no,
                                combined_ask=(yes.best_ask or Decimal("0")) + (no.best_ask or Decimal("0")),
                                max_spread=max(yes_spread, no_spread),
                            )
                        )
            if len(events) < page_size:
                break

    candidates.sort(key=_score)
    return candidates


def _leg_dict(leg: LegBook) -> dict[str, Any]:
    d = asdict(leg)
    for k, v in d.items():
        if isinstance(v, Decimal):
            d[k] = str(v)
    return d


def main() -> int:
    parser = argparse.ArgumentParser(description="M8 balanced market preflight scanner")
    parser.add_argument("--max-events", type=int, default=400)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--json-out", type=Path, help="Write top candidate JSON")
    parser.add_argument("--relaxed", action="store_true", help="Allow ask 0.20-0.80 if no balanced market")
    args = parser.parse_args()

    min_ask, max_ask = MIN_ASK, MAX_ASK
    candidates = scan_markets(max_events=args.max_events, min_ask=min_ask, max_ask=max_ask)
    if not candidates and args.relaxed:
        min_ask, max_ask = Decimal("0.20"), Decimal("0.80")
        candidates = scan_markets(max_events=args.max_events, min_ask=min_ask, max_ask=max_ask)

    print(f"M8 market preflight (position_size={POSITION_SIZE}, min_notional={MIN_NOTIONAL_USD})")
    print(f"  ask band: {min_ask}–{max_ask}, max_spread={MAX_SPREAD}, min_depth={MIN_DEPTH}")
    print(f"  candidates: {len(candidates)}")

    if not candidates:
        print("\nNo qualifying balanced market found. Try --relaxed or increase --max-events.")
        return 1

    for i, c in enumerate(candidates[: args.top], start=1):
        print(f"\n--- #{i} {c.market_question[:80]}")
        print(f"  event: {c.event_title}")
        print(f"  market_slug: {c.market_slug}")
        print(
            f"  YES ask={c.yes.best_ask} bid={c.yes.best_bid} depth={c.yes.ask_depth} "
            f"notional@5={c.yes.notional_at_size}"
        )
        print(
            f"  NO  ask={c.no.best_ask} bid={c.no.best_bid} depth={c.no.ask_depth} "
            f"notional@5={c.no.notional_at_size}"
        )
        print(f"  yes_token_id: {c.yes.token_id}")
        print(f"  no_token_id:  {c.no.token_id}")

    best = candidates[0]
    out = {
        "market_id": best.market_id,
        "market_question": best.market_question,
        "market_slug": best.market_slug,
        "event_title": best.event_title,
        "yes_token_id": best.yes.token_id,
        "no_token_id": best.no.token_id,
        "yes": _leg_dict(best.yes),
        "no": _leg_dict(best.no),
        "position_size": str(POSITION_SIZE),
        "min_notional_usd": str(MIN_NOTIONAL_USD),
    }
    if args.json_out:
        args.json_out.write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
