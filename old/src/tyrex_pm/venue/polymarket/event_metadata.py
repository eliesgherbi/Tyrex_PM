"""Resolve Polymarket event URLs into paired-binary market metadata."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

GAMMA_BASE = "https://gamma-api.polymarket.com"
_BTC_5M_SLUG_TS_RE = re.compile(r"btc-updown-5m-(\d{10,})$", re.IGNORECASE)
_YES_LABELS = frozenset({"yes", "up", "y"})
_NO_LABELS = frozenset({"no", "down", "n"})


@dataclass(frozen=True)
class EventRef:
    kind: Literal["event", "market"]
    slug: str
    market_slug: str | None = None


@dataclass(frozen=True)
class PairedBinaryEventMetadata:
    market_id: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    event_start_ts: float
    event_end_ts: float
    event_slug: str
    event_title: str
    yes_outcome_label: str
    no_outcome_label: str
    market_slug: str


class EventMetadataError(ValueError):
    """Invalid event reference or incomplete Gamma metadata."""


class EventMetadataLookupError(LookupError):
    """Gamma event/market not found."""


def parse_event_ref(ref: str) -> EventRef:
    raw = ref.strip()
    if not raw:
        raise EventMetadataError("empty event URL")

    is_url = "://" in raw or raw.startswith("/")
    if is_url:
        parsed = urlparse(raw if "://" in raw else f"https://placeholder{raw}")
        parts = [segment for segment in parsed.path.split("/") if segment]
    else:
        parts = [segment for segment in raw.split("/") if segment]

    if not parts:
        raise EventMetadataError(f"could not parse event reference: {ref!r}")

    if parts[0] in {"event", "events"}:
        if len(parts) < 2:
            raise EventMetadataError(f"event URL missing slug: {ref!r}")
        return EventRef(kind="event", slug=parts[1], market_slug=parts[2] if len(parts) > 2 else None)

    if parts[0] in {"market", "markets"}:
        if len(parts) < 2:
            raise EventMetadataError(f"market URL missing slug: {ref!r}")
        return EventRef(kind="market", slug=parts[1])

    if len(parts) >= 3 or (is_url and len(parts) >= 2):
        return EventRef(kind="event", slug=parts[-1])

    if len(parts) == 1:
        return EventRef(kind="event", slug=parts[0])

    return EventRef(kind="event", slug=parts[0], market_slug=parts[1])


def _loads_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


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


def _parse_iso_ts(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return float(text)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def btc_5m_market_id_from_end_ts(end_ts: float) -> str:
    dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)
    return dt.strftime("btc_5m_%Y%m%d_%H%M")


def _map_yes_no_tokens(pairs: list[tuple[str, str]]) -> tuple[str, str, str, str]:
    if len(pairs) != 2:
        raise EventMetadataError(f"expected binary market with 2 outcomes, got {len(pairs)}")

    yes_token: str | None = None
    no_token: str | None = None
    yes_label = ""
    no_label = ""
    for token, label in pairs:
        lower = label.lower()
        if lower in _YES_LABELS and yes_token is None:
            yes_token, yes_label = token, label
        elif lower in _NO_LABELS and no_token is None:
            no_token, no_label = token, label

    if yes_token and no_token:
        return yes_token, no_token, yes_label, no_label

    return pairs[0][0], pairs[1][0], pairs[0][1], pairs[1][1]


def _slug_start_ts(event_slug: str) -> float | None:
    match = _BTC_5M_SLUG_TS_RE.search(event_slug)
    if not match:
        return None
    return float(match.group(1))


def _select_market(
    event: dict[str, Any],
    *,
    market_slug: str | None,
) -> dict[str, Any]:
    markets = event.get("markets") if isinstance(event.get("markets"), list) else []
    selected = [market for market in markets if isinstance(market, dict)]
    if not selected:
        raise EventMetadataError("event has no markets")

    if market_slug:
        needle = market_slug.lower()
        filtered = [
            market
            for market in selected
            if str(market.get("slug", "")).lower() == needle
            or str(market.get("id", "")).lower() == needle
        ]
        if not filtered:
            raise EventMetadataError(f"no market matched slug={market_slug!r}")
        return filtered[0]

    if len(selected) == 1:
        return selected[0]

    tradeable = [
        market
        for market in selected
        if market.get("enableOrderBook") is True and _iter_market_outcomes(market)
    ]
    if len(tradeable) == 1:
        return tradeable[0]

    raise EventMetadataError(
        f"event has {len(selected)} markets; pass a URL with an explicit market slug"
    )


def _metadata_from_event_market(
    event: dict[str, Any],
    market: dict[str, Any],
) -> PairedBinaryEventMetadata:
    pairs = _iter_market_outcomes(market)
    if not pairs:
        raise EventMetadataError("market missing clobTokenIds/outcomes")

    yes_token_id, no_token_id, yes_label, no_label = _map_yes_no_tokens(pairs)
    condition_id = str(market.get("conditionId") or market.get("condition_id") or "").strip()
    if not condition_id:
        raise EventMetadataError("market missing conditionId")

    event_slug = str(event.get("slug", "")).strip()
    start_ts = (
        _parse_iso_ts(market.get("eventStartTime"))
        or _parse_iso_ts(event.get("startTime"))
        or _parse_iso_ts(market.get("startDate"))
        or _parse_iso_ts(event.get("startDate"))
        or _slug_start_ts(event_slug)
    )
    end_ts = (
        _parse_iso_ts(market.get("endDate"))
        or _parse_iso_ts(event.get("endDate"))
        or (start_ts + 300.0 if start_ts is not None else None)
    )
    if start_ts is None:
        raise EventMetadataError("could not resolve event_start_ts from Gamma metadata")
    if end_ts is None:
        raise EventMetadataError("could not resolve event_end_ts from Gamma metadata")
    if end_ts <= start_ts:
        raise EventMetadataError(
            f"resolved event_end_ts ({end_ts}) must be > event_start_ts ({start_ts})"
        )

    return PairedBinaryEventMetadata(
        market_id=btc_5m_market_id_from_end_ts(end_ts),
        condition_id=condition_id,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        event_start_ts=start_ts,
        event_end_ts=end_ts,
        event_slug=event_slug,
        event_title=str(event.get("title", "")),
        yes_outcome_label=yes_label,
        no_outcome_label=no_label,
        market_slug=str(market.get("slug", "")),
    )


def _fetch_event_by_slug(client: httpx.Client, *, gamma_base: str, slug: str) -> dict[str, Any]:
    resp = client.get(f"{gamma_base.rstrip('/')}/events/slug/{slug}")
    if resp.status_code == 404:
        raise EventMetadataLookupError(f"no Gamma event for slug={slug!r}")
    resp.raise_for_status()
    event = resp.json()
    if not isinstance(event, dict):
        raise EventMetadataError(f"Gamma event/slug/{slug} returned non-object")
    return event


def _fetch_market_by_slug(client: httpx.Client, *, gamma_base: str, slug: str) -> dict[str, Any]:
    resp = client.get(f"{gamma_base.rstrip('/')}/markets/slug/{slug}")
    if resp.status_code == 404:
        raise EventMetadataLookupError(f"no Gamma market for slug={slug!r}")
    resp.raise_for_status()
    market = resp.json()
    if not isinstance(market, dict):
        raise EventMetadataError(f"Gamma market/slug/{slug} returned non-object")
    return market


def _resolve_event_record(
    client: httpx.Client,
    *,
    gamma_base: str,
    event_ref: EventRef,
) -> tuple[dict[str, Any], str | None]:
    if event_ref.kind == "event":
        return _fetch_event_by_slug(client, gamma_base=gamma_base, slug=event_ref.slug), event_ref.market_slug

    market = _fetch_market_by_slug(client, gamma_base=gamma_base, slug=event_ref.slug)
    nested = market.get("events")
    if isinstance(nested, list) and nested and isinstance(nested[0], dict):
        parent_slug = str(nested[0].get("slug", "")).strip()
        if parent_slug:
            event = _fetch_event_by_slug(client, gamma_base=gamma_base, slug=parent_slug)
            return event, str(market.get("slug", "")).strip() or event_ref.slug

    return {
        "title": str(market.get("question", market.get("title", ""))),
        "slug": str(market.get("slug", event_ref.slug)),
        "markets": [market],
    }, str(market.get("slug", "")).strip() or event_ref.slug


def _fetch_market_by_token(client: httpx.Client, *, gamma_base: str, token_id: str) -> dict[str, Any]:
    resp = client.get(
        f"{gamma_base.rstrip('/')}/markets",
        params={"clob_token_ids": str(token_id).strip()},
    )
    resp.raise_for_status()
    rows = resp.json()
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise EventMetadataLookupError(f"no Gamma market for token_id={token_id!r}")
    return rows[0]


def resolve_paired_binary_event_metadata(
    event_url: str,
    *,
    gamma_base: str = GAMMA_BASE,
    timeout_s: float = 15.0,
) -> PairedBinaryEventMetadata:
    event_ref = parse_event_ref(event_url)
    with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
        event, market_slug = _resolve_event_record(
            client,
            gamma_base=gamma_base,
            event_ref=event_ref,
        )
        market = _select_market(event, market_slug=market_slug)
        return _metadata_from_event_market(event, market)


def resolve_paired_binary_from_tokens(
    yes_token_id: str,
    no_token_id: str,
    *,
    gamma_base: str = GAMMA_BASE,
    timeout_s: float = 15.0,
) -> PairedBinaryEventMetadata:
    yes = str(yes_token_id).strip()
    no = str(no_token_id).strip()
    if not yes or not no:
        raise EventMetadataError("yes_token_id and no_token_id are required")

    with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
        market = _fetch_market_by_token(client, gamma_base=gamma_base, token_id=yes)
        pairs = _iter_market_outcomes(market)
        token_set = {token for token, _ in pairs}
        if no not in token_set:
            other = _fetch_market_by_token(client, gamma_base=gamma_base, token_id=no)
            if str(other.get("conditionId", "")) != str(market.get("conditionId", "")):
                raise EventMetadataError("yes/no token ids do not belong to the same market")

        event = {
            "title": str(market.get("question", market.get("title", ""))),
            "slug": str(market.get("slug", "")),
            "startTime": market.get("eventStartTime") or market.get("startDate"),
            "endDate": market.get("endDate"),
            "markets": [market],
        }
        return _metadata_from_event_market(event, market)
