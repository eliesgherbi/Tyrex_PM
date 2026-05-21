"""Find a currently tradeable sports token_id for live smoke tests.

The script scans Polymarket Gamma events, filters to sports-tagged active
markets that accept CLOB orders, then verifies the chosen outcome token against
the CLOB /book endpoint. It prints the token_id by default so it can be pasted
into strategy YAML.

Run from the repo root:

    python scripts/get_token_id.py
    python scripts/get_token_id.py --outcome no --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx


GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
SPORTS_TERMS = {
    "sports",
    "soccer",
    "nba",
    "nfl",
    "mlb",
    "nhl",
    "ufc",
    "mma",
    "tennis",
    "basketball",
    "baseball",
    "football",
    "hockey",
    "golf",
    "fifa",
}


@dataclass(frozen=True)
class Candidate:
    token_id: str
    outcome: str
    event_title: str
    market_question: str
    market_slug: str
    best_bid: Decimal | None
    best_ask: Decimal | None


def _loads_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number > 0 else None


def _best_book_levels(book: Any) -> tuple[Decimal | None, Decimal | None]:
    if not isinstance(book, dict):
        return None, None

    bids: list[Decimal] = []
    asks: list[Decimal] = []
    for key, out in (("bids", bids), ("asks", asks)):
        levels = book.get(key)
        if not isinstance(levels, list):
            continue
        for level in levels:
            if not isinstance(level, dict) or _positive_decimal(level.get("size")) is None:
                continue
            price = _positive_decimal(level.get("price"))
            if price is not None:
                out.append(price)

    return max(bids, default=None), min(asks, default=None)


def _event_looks_like_sports(event: dict[str, Any], extra_terms: set[str]) -> bool:
    terms = SPORTS_TERMS | {term.lower() for term in extra_terms}
    tags = event.get("tags") if isinstance(event.get("tags"), list) else []
    tag_text = " ".join(
        f"{tag.get('slug', '')} {tag.get('label', '')}".lower()
        for tag in tags
        if isinstance(tag, dict)
    )
    event_text = f"{event.get('title', '')} {event.get('slug', '')} {tag_text}".lower()
    return any(term in event_text for term in terms)


def _market_is_tradeable(market: dict[str, Any]) -> bool:
    return (
        market.get("active") is True
        and market.get("closed") is not True
        and market.get("archived") is not True
        and market.get("acceptingOrders") is True
        and market.get("enableOrderBook") is True
    )


def _iter_market_outcomes(market: dict[str, Any]) -> list[tuple[str, str]]:
    token_ids = _loads_jsonish(market.get("clobTokenIds"))
    outcomes = _loads_jsonish(market.get("outcomes"))
    if not isinstance(token_ids, list) or not isinstance(outcomes, list):
        return []

    pairs: list[tuple[str, str]] = []
    for token_id, outcome in zip(token_ids, outcomes, strict=False):
        token = str(token_id).strip()
        label = str(outcome).strip()
        if token and label:
            pairs.append((token, label))
    return pairs


def _outcome_matches(label: str, requested: str) -> bool:
    return requested == "any" or label.lower() == requested


def _book_satisfies(best_bid: Decimal | None, best_ask: Decimal | None, required: str) -> bool:
    if required == "none":
        return True
    if required == "any":
        return best_bid is not None or best_ask is not None
    if required == "bid":
        return best_bid is not None
    if required == "ask":
        return best_ask is not None
    if required == "both":
        return best_bid is not None and best_ask is not None
    raise ValueError(f"unknown book side requirement: {required}")


def _event_sort_key(event: dict[str, Any]) -> tuple[Decimal, str]:
    liquidity = _positive_decimal(event.get("liquidityClob") or event.get("liquidity"))
    volume_24h = _positive_decimal(event.get("volume24hr") or 0)
    return (liquidity or Decimal("0")) + (volume_24h or Decimal("0")), str(event.get("title", ""))


def find_candidate(
    *,
    outcome: str,
    require_book_side: str,
    max_events: int,
    page_size: int,
    gamma_base: str,
    clob_base: str,
    extra_terms: set[str],
    timeout_s: float,
) -> Candidate | None:
    gamma_base = gamma_base.rstrip("/")
    clob_base = clob_base.rstrip("/")
    seen = 0

    with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
        for offset in range(0, max_events, page_size):
            resp = client.get(
                f"{gamma_base}/events",
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
                return None

            sports_events = [
                event
                for event in events
                if isinstance(event, dict) and _event_looks_like_sports(event, extra_terms)
            ]
            sports_events.sort(key=_event_sort_key, reverse=True)

            for event in sports_events:
                seen += 1
                markets = event.get("markets") if isinstance(event.get("markets"), list) else []
                for market in markets:
                    if not isinstance(market, dict) or not _market_is_tradeable(market):
                        continue
                    for token_id, label in _iter_market_outcomes(market):
                        if not _outcome_matches(label, outcome):
                            continue
                        book_resp = client.get(f"{clob_base}/book", params={"token_id": token_id})
                        if book_resp.status_code != 200:
                            continue
                        best_bid, best_ask = _best_book_levels(book_resp.json())
                        if not _book_satisfies(best_bid, best_ask, require_book_side):
                            continue
                        return Candidate(
                            token_id=token_id,
                            outcome=label,
                            event_title=str(event.get("title", "")),
                            market_question=str(market.get("question", "")),
                            market_slug=str(market.get("slug", "")),
                            best_bid=best_bid,
                            best_ask=best_ask,
                        )

            if len(events) < page_size:
                return None

    return None


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print a valid Polymarket sports outcome token_id for live tests."
    )
    parser.add_argument(
        "--outcome",
        choices=("yes", "no", "any"),
        default="any",
        help="Preferred binary outcome label. Default: any.",
    )
    parser.add_argument(
        "--require-book-side",
        choices=("none", "any", "bid", "ask", "both"),
        default="ask",
        help="Liquidity check required on /book. Default: ask for BUY smoke tests.",
    )
    parser.add_argument(
        "--term",
        action="append",
        default=[],
        help="Additional sports/event term to match. Can be passed more than once.",
    )
    parser.add_argument("--max-events", type=int, default=500, help="Maximum events to scan.")
    parser.add_argument("--page-size", type=int, default=100, help="Gamma events page size.")
    parser.add_argument("--timeout-s", type=float, default=15.0, help="HTTP timeout in seconds.")
    parser.add_argument("--gamma-base", default=GAMMA_BASE, help="Gamma API base URL.")
    parser.add_argument("--clob-base", default=CLOB_BASE, help="CLOB API base URL.")
    parser.add_argument("--json", action="store_true", help="Print full candidate as JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        candidate = find_candidate(
            outcome=args.outcome,
            require_book_side=args.require_book_side,
            max_events=args.max_events,
            page_size=args.page_size,
            gamma_base=args.gamma_base,
            clob_base=args.clob_base,
            extra_terms=set(args.term),
            timeout_s=args.timeout_s,
        )
    except httpx.HTTPError as e:
        print(f"HTTP error while searching for token_id: {e!r}", file=sys.stderr)
        return 2

    if candidate is None:
        print("No active sports token_id found with the requested filters.", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "token_id": candidate.token_id,
                    "outcome": candidate.outcome,
                    "event_title": candidate.event_title,
                    "market_question": candidate.market_question,
                    "market_slug": candidate.market_slug,
                    "best_bid": str(candidate.best_bid) if candidate.best_bid is not None else None,
                    "best_ask": str(candidate.best_ask) if candidate.best_ask is not None else None,
                },
                indent=2,
            )
        )
    else:
        print(candidate.token_id)
        print(f"outcome={candidate.outcome}", file=sys.stderr)
        print(f"event={candidate.event_title}", file=sys.stderr)
        print(f"market={candidate.market_question}", file=sys.stderr)
        print(f"best_bid={candidate.best_bid} best_ask={candidate.best_ask}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
