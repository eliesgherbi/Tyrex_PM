"""Market discovery: fixture JSON and public Gamma API (read-only)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from tyrex_pm.core.ids import MarketId, TokenId
from tyrex_pm.domain.polymarket.market import (
    BinaryMarket,
    MarketRequest,
    MarketStatus,
    make_binary_instruments,
)

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text).astimezone(timezone.utc)


MappingLike = dict[str, Any]


def _status(raw: MappingLike) -> MarketStatus:
    if raw.get("closed") is True:
        return MarketStatus.CLOSED
    if raw.get("active") is True:
        return MarketStatus.ACTIVE
    return MarketStatus.UNKNOWN


def market_from_fixture_dict(data: MappingLike) -> BinaryMarket:
    market = data.get("market") or data
    condition_id = str(market["condition_id"])
    market_id = MarketId(str(market.get("market_id") or condition_id))
    yes_token = TokenId(str(market["yes_token_id"]))
    no_token = TokenId(str(market["no_token_id"]))
    yes, no = make_binary_instruments(
        market_id=market_id, yes_token=yes_token, no_token=no_token
    )
    tick = market.get("tick_size")
    min_size = market.get("min_order_size")
    return BinaryMarket(
        market_id=market_id,
        condition_id=condition_id,
        question=str(market.get("question") or ""),
        yes=yes,
        no=no,
        event_start=_parse_dt(market.get("event_start")),
        event_end=_parse_dt(market.get("event_end")),
        tick_size=None if tick is None else Decimal(str(tick)),
        min_order_size=None if min_size is None else Decimal(str(min_size)),
        status=MarketStatus(str(market.get("status", "UNKNOWN"))),
        event_slug=market.get("event_slug"),
    )


def load_market_from_fixture(path: Path | str) -> BinaryMarket:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return market_from_fixture_dict(payload)


def slug_from_request(request: MarketRequest) -> str | None:
    if request.event_slug:
        return request.event_slug
    if request.event_url:
        path = urlparse(request.event_url).path.rstrip("/")
        return path.split("/")[-1] or None
    return None


def market_from_gamma_event(event: MappingLike, *, condition_id: str | None = None) -> BinaryMarket:
    markets = event.get("markets") or []
    if not markets:
        raise ValueError("gamma event has no markets")
    selected = None
    if condition_id:
        for m in markets:
            if str(m.get("conditionId") or m.get("condition_id")) == condition_id:
                selected = m
                break
        if selected is None:
            raise ValueError(f"condition_id not found: {condition_id}")
    else:
        selected = markets[0]

    cid = str(selected.get("conditionId") or selected.get("condition_id"))
    # clobTokenIds may be JSON string
    token_ids = selected.get("clobTokenIds") or selected.get("clob_token_ids")
    if isinstance(token_ids, str):
        token_ids = json.loads(token_ids)
    if not token_ids or len(token_ids) < 2:
        raise ValueError("market missing clobTokenIds")
    outcomes = selected.get("outcomes")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    yes_idx, no_idx = 0, 1
    if outcomes and len(outcomes) >= 2:
        lowered = [str(o).lower() for o in outcomes]
        if "yes" in lowered and "no" in lowered:
            yes_idx = lowered.index("yes")
            no_idx = lowered.index("no")
        elif "up" in lowered and "down" in lowered:
            yes_idx = lowered.index("up")
            no_idx = lowered.index("down")

    market_id = MarketId(cid)
    yes, no = make_binary_instruments(
        market_id=market_id,
        yes_token=TokenId(str(token_ids[yes_idx])),
        no_token=TokenId(str(token_ids[no_idx])),
    )
    tick = selected.get("orderPriceMinTickSize") or selected.get("minimum_tick_size")
    min_size = selected.get("orderMinSize") or selected.get("minimum_order_size")
    return BinaryMarket(
        market_id=market_id,
        condition_id=cid,
        question=str(selected.get("question") or event.get("title") or ""),
        yes=yes,
        no=no,
        event_start=_parse_dt(selected.get("eventStartTime") or event.get("startTime")),
        event_end=_parse_dt(selected.get("endDate") or event.get("endDate")),
        tick_size=None if tick is None else Decimal(str(tick)),
        min_order_size=None if min_size is None else Decimal(str(min_size)),
        status=_status(selected),
        event_slug=str(event.get("slug") or ""),
    )


class FixtureMarketDiscovery:
    async def resolve_market(self, request: MarketRequest) -> BinaryMarket:
        if not request.fixture_path:
            raise ValueError("FixtureMarketDiscovery requires fixture_path")
        return load_market_from_fixture(request.fixture_path)


def _http_get_json(url: str, *, opener=urlopen, timeout: float = 30.0) -> Any:
    req = Request(
        url,
        headers={
            "User-Agent": "tyrex-pm/0.3 (read-only observe; +https://github.com/local/tyrex-pm)",
            "Accept": "application/json",
        },
    )
    with opener(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class GammaMarketDiscovery:
    """Public Gamma HTTP discovery (no auth)."""

    def __init__(self, *, base_url: str = GAMMA_EVENTS_URL, opener=urlopen) -> None:
        self._base_url = base_url
        self._opener = opener

    async def resolve_market(self, request: MarketRequest) -> BinaryMarket:
        slug = slug_from_request(request)
        if slug:
            url = f"{self._base_url}?slug={slug}"
            data = _http_get_json(url, opener=self._opener)
            if isinstance(data, list):
                if not data:
                    raise ValueError(f"no gamma event for slug={slug}")
                event = data[0]
            else:
                event = data
            return market_from_gamma_event(event, condition_id=request.condition_id)
        if request.condition_id:
            # Fallback search — gamma markets endpoint
            url = f"https://gamma-api.polymarket.com/markets?condition_ids={request.condition_id}"
            markets = _http_get_json(url, opener=self._opener)
            if not markets:
                raise ValueError(f"no market for condition_id={request.condition_id}")
            m = markets[0]
            event = {"slug": m.get("slug"), "markets": [m], "title": m.get("question")}
            return market_from_gamma_event(event, condition_id=request.condition_id)
        raise ValueError("GammaMarketDiscovery needs event_slug, event_url, or condition_id")


_SLUG_TS = re.compile(r"(\d{10})$")


def next_btc_updown_slug(now: datetime | None = None) -> str:
    """BTC Up/Down 5m window slug for the next boundary (operations boundary)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    epoch = int(now.timestamp())
    window = 300
    next_start = ((epoch // window) + 1) * window
    return f"btc-updown-5m-{next_start}"


def current_btc_updown_slug(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    epoch = int(now.timestamp())
    window = 300
    start = (epoch // window) * window
    return f"btc-updown-5m-{start}"
