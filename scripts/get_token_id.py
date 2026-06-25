"""Find a currently tradeable outcome token_id for live smoke tests.

By default the script scans Polymarket Gamma events, filters to sports-tagged
active markets that accept CLOB orders, then verifies the chosen outcome token
against the CLOB /book endpoint. It prints the token_id by default so it can be
pasted into strategy YAML.

Pass ``--event`` with a Polymarket URL or slug to resolve that event (or a single
market under it) instead of scanning sports listings.

Run from the repo root:

    python scripts/get_token_id.py
    python scripts/get_token_id.py --outcome no --json
    python scripts/get_token_id.py --event "https://polymarket.com/event/my-event-slug"
    python scripts/get_token_id.py --event "https://polymarket.com/esports/cs2/.../match-slug"
    python scripts/get_token_id.py --event my-event-slug --require-book-side none
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlparse

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
class EventRef:
    """Parsed ``--event`` value: event slug, optional nested market slug, or market-only."""

    kind: Literal["event", "market"]
    slug: str
    market_slug: str | None = None


@dataclass(frozen=True)
class Candidate:
    token_id: str
    outcome: str
    event_title: str
    event_slug: str
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


def _parse_event_ref(ref: str) -> EventRef:
    """Parse a Polymarket URL, path, or bare slug into an :class:`EventRef`."""
    raw = ref.strip()
    if not raw:
        raise ValueError("empty --event value")

    is_url = "://" in raw or raw.startswith("/")
    if is_url:
        parsed = urlparse(raw if "://" in raw else f"https://placeholder{raw}")
        parts = [segment for segment in parsed.path.split("/") if segment]
    else:
        parts = [segment for segment in raw.split("/") if segment]

    if not parts:
        raise ValueError(f"could not parse event reference: {ref!r}")

    if parts[0] in {"event", "events"}:
        if len(parts) < 2:
            raise ValueError(f"event URL missing slug: {ref!r}")
        return EventRef(kind="event", slug=parts[1], market_slug=parts[2] if len(parts) > 2 else None)

    if parts[0] in {"market", "markets"}:
        if len(parts) < 2:
            raise ValueError(f"market URL missing slug: {ref!r}")
        return EventRef(kind="market", slug=parts[1])

    # Category front-door URLs omit /event/: /esports/cs2/.../<slug>
    if len(parts) >= 3 or (is_url and len(parts) >= 2):
        return EventRef(kind="event", slug=parts[-1])

    if len(parts) == 1:
        return EventRef(kind="event", slug=parts[0])

    return EventRef(kind="event", slug=parts[0], market_slug=parts[1])


def _fetch_event_by_slug(
    client: httpx.Client,
    *,
    gamma_base: str,
    slug: str,
) -> dict[str, Any] | None:
    resp = client.get(f"{gamma_base}/events/slug/{slug}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    event = resp.json()
    return event if isinstance(event, dict) else None


def _fetch_market_by_slug(
    client: httpx.Client,
    *,
    gamma_base: str,
    slug: str,
) -> dict[str, Any] | None:
    resp = client.get(f"{gamma_base}/markets/slug/{slug}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    market = resp.json()
    return market if isinstance(market, dict) else None


def _event_from_market_record(market: dict[str, Any]) -> dict[str, Any] | None:
    nested = market.get("events")
    if isinstance(nested, list) and nested and isinstance(nested[0], dict):
        return nested[0]
    return None


def _resolve_event_ref(
    client: httpx.Client,
    *,
    gamma_base: str,
    event_ref: EventRef,
) -> tuple[dict[str, Any], str | None]:
    """Return (event dict, optional market slug filter)."""
    if event_ref.kind == "event":
        event = _fetch_event_by_slug(client, gamma_base=gamma_base, slug=event_ref.slug)
        if event is None:
            raise LookupError(f"no Gamma event for slug={event_ref.slug!r}")
        return event, event_ref.market_slug

    market = _fetch_market_by_slug(client, gamma_base=gamma_base, slug=event_ref.slug)
    if market is None:
        raise LookupError(f"no Gamma market for slug={event_ref.slug!r}")

    parent = _event_from_market_record(market)
    if parent is not None:
        event_slug = str(parent.get("slug", "")).strip()
        if event_slug:
            event = _fetch_event_by_slug(client, gamma_base=gamma_base, slug=event_slug)
            if event is not None:
                return event, str(market.get("slug", "")).strip() or event_ref.slug

    return {
        "title": str(market.get("question", market.get("title", ""))),
        "slug": str(market.get("slug", event_ref.slug)),
        "markets": [market],
    }, str(market.get("slug", "")).strip() or event_ref.slug


def _iter_target_markets(
    event: dict[str, Any],
    *,
    market_slug: str | None,
) -> list[dict[str, Any]]:
    markets = event.get("markets") if isinstance(event.get("markets"), list) else []
    selected = [market for market in markets if isinstance(market, dict)]
    if not market_slug:
        return selected
    needle = market_slug.lower()
    filtered = [
        market
        for market in selected
        if str(market.get("slug", "")).lower() == needle
        or str(market.get("id", "")).lower() == needle
    ]
    return filtered


def _scan_events_for_candidate(
    client: httpx.Client,
    *,
    events: list[dict[str, Any]],
    outcome: str,
    require_book_side: str,
    clob_base: str,
    market_slug: str | None,
    relax_market_filters: bool = False,
) -> Candidate | None:
    for event in events:
        if not isinstance(event, dict):
            continue
        for market in _iter_target_markets(event, market_slug=market_slug):
            if not relax_market_filters and not _market_is_tradeable(market):
                continue
            if relax_market_filters and not _iter_market_outcomes(market):
                continue
            for token_id, label in _iter_market_outcomes(market):
                if not _outcome_matches(label, outcome):
                    continue
                book_resp = client.get(f"{clob_base}/book", params={"token_id": token_id})
                if book_resp.status_code == 200:
                    best_bid, best_ask = _best_book_levels(book_resp.json())
                elif relax_market_filters and require_book_side == "none":
                    best_bid, best_ask = None, None
                else:
                    continue
                if not _book_satisfies(best_bid, best_ask, require_book_side):
                    continue
                return Candidate(
                    token_id=token_id,
                    outcome=label,
                    event_title=str(event.get("title", "")),
                    event_slug=str(event.get("slug", "")),
                    market_question=str(market.get("question", "")),
                    market_slug=str(market.get("slug", "")),
                    best_bid=best_bid,
                    best_ask=best_ask,
                )
    return None


def find_candidate_for_event(
    *,
    event: str,
    outcome: str,
    require_book_side: str,
    gamma_base: str,
    clob_base: str,
    timeout_s: float,
) -> Candidate | None:
    gamma_base = gamma_base.rstrip("/")
    clob_base = clob_base.rstrip("/")
    event_ref = _parse_event_ref(event)

    with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
        resolved, market_slug = _resolve_event_ref(
            client,
            gamma_base=gamma_base,
            event_ref=event_ref,
        )
        candidate = _scan_events_for_candidate(
            client,
            events=[resolved],
            outcome=outcome,
            require_book_side=require_book_side,
            clob_base=clob_base,
            market_slug=market_slug,
            relax_market_filters=False,
        )
        if candidate is not None:
            return candidate
        return _scan_events_for_candidate(
            client,
            events=[resolved],
            outcome=outcome,
            require_book_side=require_book_side,
            clob_base=clob_base,
            market_slug=market_slug,
            relax_market_filters=True,
        )


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
                candidate = _scan_events_for_candidate(
                    client,
                    events=[event],
                    outcome=outcome,
                    require_book_side=require_book_side,
                    clob_base=clob_base,
                    market_slug=None,
                )
                if candidate is not None:
                    return candidate

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
        "--event",
        metavar="URL_OR_SLUG",
        default=None,
        help=(
            "Focus on one Polymarket event: full URL (/event/<slug> or category paths "
            "like /esports/.../<slug>), /market/<slug>, or bare event slug."
        ),
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
        if args.event:
            candidate = find_candidate_for_event(
                event=args.event,
                outcome=args.outcome,
                require_book_side=args.require_book_side,
                gamma_base=args.gamma_base,
                clob_base=args.clob_base,
                timeout_s=args.timeout_s,
            )
        else:
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
    except ValueError as e:
        print(f"Invalid --event value: {e}", file=sys.stderr)
        return 2
    except LookupError as e:
        print(str(e), file=sys.stderr)
        return 1
    except httpx.HTTPError as e:
        print(f"HTTP error while searching for token_id: {e!r}", file=sys.stderr)
        return 2

    if candidate is None:
        if args.event:
            print(
                "No outcome token_id found for that event with the requested filters. "
                "For closed markets use --require-book-side none; try --outcome yes|no.",
                file=sys.stderr,
            )
        else:
            print("No active sports token_id found with the requested filters.", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "token_id": candidate.token_id,
                    "outcome": candidate.outcome,
                    "event_title": candidate.event_title,
                    "event_slug": candidate.event_slug,
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
        print(f"event_slug={candidate.event_slug}", file=sys.stderr)
        print(f"market={candidate.market_question}", file=sys.stderr)
        print(f"best_bid={candidate.best_bid} best_ask={candidate.best_ask}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
