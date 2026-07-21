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

from tyrex_pm.adapters.polymarket.btc_5m_window import (
    BTC_5M_WINDOW_S,
    parse_slug_epoch,
    window_from_slug_epoch,
)
from tyrex_pm.core.ids import MarketId, TokenId
from tyrex_pm.domain.polymarket.discovery_binding import (
    DiscoveredMarketBinding,
    DiscoverySessionRole,
)
from tyrex_pm.domain.polymarket.market import (
    BinaryMarket,
    MarketRequest,
    MarketStatus,
    make_binary_instruments,
)
from tyrex_pm.domain.polymarket.outcome_map import (
    NormalizedLeg,
    OutcomeMapError,
    map_up_down_outcomes,
    map_yes_no_outcomes,
    rule_fingerprint,
)

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CHAINLINK_BTC_USD_STREAM = "https://data.chain.link/streams/btc-usd"
_BTC_5M_SLUG = re.compile(r"^btc-updown-5m-(\d{10,})$", re.IGNORECASE)


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


def _select_market(
    event: MappingLike, *, condition_id: str | None = None
) -> MappingLike:
    markets = event.get("markets") or []
    if not markets:
        raise ValueError("gamma event has no markets")
    if condition_id:
        for m in markets:
            if str(m.get("conditionId") or m.get("condition_id")) == condition_id:
                return m
        raise ValueError(f"condition_id not found: {condition_id}")
    return markets[0]


def _binary_market_from_selected(
    event: MappingLike,
    selected: MappingLike,
    *,
    up_token: TokenId,
    down_token: TokenId,
) -> BinaryMarket:
    """Build BinaryMarket with YES←Up and NO←Down instrument slots (legacy fields)."""
    cid = str(selected.get("conditionId") or selected.get("condition_id"))
    if not cid:
        raise ValueError("incomplete_gamma: missing conditionId")
    market_id = MarketId(cid)
    yes, no = make_binary_instruments(
        market_id=market_id,
        yes_token=up_token,
        no_token=down_token,
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


def market_from_gamma_event(
    event: MappingLike, *, condition_id: str | None = None
) -> BinaryMarket:
    """Legacy path: map Up/Down or Yes/No by label into BinaryMarket yes/no slots."""
    selected = _select_market(event, condition_id=condition_id)
    outcomes = selected.get("outcomes")
    token_ids = selected.get("clobTokenIds") or selected.get("clob_token_ids")
    if outcomes is None or token_ids is None:
        raise ValueError("incomplete_gamma: missing outcomes or clobTokenIds")
    labels = outcomes if not isinstance(outcomes, str) else json.loads(outcomes)
    lowered = [str(o).lower() for o in labels]
    try:
        if "up" in lowered and "down" in lowered:
            om = map_up_down_outcomes(outcomes, token_ids)
            up_t, down_t = om.as_up_down()
            return _binary_market_from_selected(
                event, selected, up_token=up_t, down_token=down_t
            )
        if "yes" in lowered and "no" in lowered:
            om = map_yes_no_outcomes(outcomes, token_ids)
            yes_t, no_t = om.as_yes_no()
            return _binary_market_from_selected(
                event, selected, up_token=yes_t, down_token=no_t
            )
    except OutcomeMapError as exc:
        raise ValueError(str(exc)) from exc
    raise ValueError(
        "outcome_map_rejected: positional mapping forbidden; "
        "need label-based Up/Down or Yes/No"
    )


def bind_btc_5m_gamma_event(
    event: MappingLike,
    *,
    expected_slug: str | None = None,
    expected_window_start: datetime | None = None,
    condition_id: str | None = None,
    session_role: DiscoverySessionRole = DiscoverySessionRole.CANDIDATE,
    require_chainlink_source: bool = True,
) -> DiscoveredMarketBinding:
    """Validate Gamma event as BTC 5m Up/Down and return a binding.

    Rejects wrong window, incomplete metadata, bad outcome maps, and
    resolution-source mismatch when ``require_chainlink_source`` is set.
    """
    slug = str(event.get("slug") or "")
    if expected_slug and slug != expected_slug:
        raise ValueError(f"wrong_window: slug={slug!r} expected={expected_slug!r}")
    if not _BTC_5M_SLUG.match(slug):
        raise ValueError(f"wrong_window: not a btc-updown-5m slug: {slug!r}")

    selected = _select_market(event, condition_id=condition_id)
    outcomes = selected.get("outcomes")
    token_ids = selected.get("clobTokenIds") or selected.get("clob_token_ids")
    if outcomes is None or token_ids is None:
        raise ValueError("incomplete_gamma: missing outcomes or clobTokenIds")
    try:
        om = map_up_down_outcomes(outcomes, token_ids)
    except OutcomeMapError as exc:
        raise ValueError(str(exc)) from exc

    up_t, down_t = om.as_up_down()
    market = _binary_market_from_selected(
        event, selected, up_token=up_t, down_token=down_t
    )

    slug_epoch = parse_slug_epoch(slug)
    start, end = window_from_slug_epoch(slug, slug_epoch)
    if expected_window_start is not None:
        if abs((start - expected_window_start).total_seconds()) >= 1.0:
            raise ValueError(
                f"wrong_window: slug epoch {start.isoformat()} != "
                f"requested {expected_window_start.isoformat()}"
            )
    if market.event_start is not None:
        if abs((market.event_start - start).total_seconds()) >= 1.0:
            raise ValueError(
                f"wrong_window: eventStartTime {market.event_start.isoformat()} "
                f"!= slug epoch {start.isoformat()}"
            )
    if market.event_end is not None:
        if abs((market.event_end - end).total_seconds()) >= 1.0:
            raise ValueError(
                f"stale_or_mismatched_end: endDate {market.event_end.isoformat()} "
                f"!= expected {end.isoformat()}"
            )

    # Prefer slug-authoritative window on the binding's market copy
    market = BinaryMarket(
        market_id=market.market_id,
        condition_id=market.condition_id,
        question=market.question,
        yes=market.yes,
        no=market.no,
        event_start=start,
        event_end=end,
        tick_size=market.tick_size,
        min_order_size=market.min_order_size,
        status=market.status,
        event_slug=slug,
    )

    resolution_source = str(
        selected.get("resolutionSource")
        or selected.get("resolution_source")
        or event.get("resolutionSource")
        or ""
    ).strip()
    description = str(selected.get("description") or event.get("description") or "")
    rule_ok = True
    if require_chainlink_source:
        if CHAINLINK_BTC_USD_STREAM not in resolution_source and (
            "chainlink" not in resolution_source.lower()
            or "btc" not in (resolution_source + description).lower()
        ):
            rule_ok = False
            raise ValueError(
                f"market_rule_source_mismatch: resolutionSource={resolution_source!r}"
            )
        if "chainlink" not in description.lower() and CHAINLINK_BTC_USD_STREAM not in description:
            # Soft: Gamma description usually cites Chainlink; if absent but
            # resolutionSource is exact, still accept.
            if resolution_source.rstrip("/") != CHAINLINK_BTC_USD_STREAM.rstrip("/"):
                rule_ok = False
                raise ValueError("market_rule_source_mismatch: description lacks Chainlink")

    fp = rule_fingerprint(resolution_source=resolution_source, description=description)
    return DiscoveredMarketBinding(
        market=market,
        outcomes=om,
        session_role=session_role,
        window_slug=slug,
        resolution_source=resolution_source or None,
        resolution_rule_fingerprint=fp,
        market_rule_ok=rule_ok,
        requested_window_start=expected_window_start or start,
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
        binding = await self.resolve_binding(
            request, session_role=DiscoverySessionRole.ACTIVE
        )
        return binding.market

    async def resolve_binding(
        self,
        request: MarketRequest,
        *,
        session_role: DiscoverySessionRole = DiscoverySessionRole.CANDIDATE,
        require_btc_5m_rules: bool | None = None,
    ) -> DiscoveredMarketBinding:
        """Resolve and validate; suitable for active or prepared-next."""
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
            is_btc = bool(_BTC_5M_SLUG.match(slug))
            use_btc = require_btc_5m_rules if require_btc_5m_rules is not None else is_btc
            if use_btc:
                expected_start = None
                try:
                    expected_start = window_from_slug_epoch(slug)[0]
                except Exception:
                    expected_start = None
                return bind_btc_5m_gamma_event(
                    event,
                    expected_slug=slug,
                    expected_window_start=expected_start,
                    condition_id=request.condition_id,
                    session_role=session_role,
                )
            # Non-BTC binary: label Yes/No or Up/Down into BinaryMarket
            market = market_from_gamma_event(event, condition_id=request.condition_id)
            selected = _select_market(event, condition_id=request.condition_id)
            outcomes = selected.get("outcomes")
            tokens = selected.get("clobTokenIds") or selected.get("clob_token_ids")
            lowered = [
                str(o).lower()
                for o in (
                    outcomes if not isinstance(outcomes, str) else json.loads(outcomes)
                )
            ]
            if "up" in lowered:
                om = map_up_down_outcomes(outcomes, tokens)
            else:
                om = map_yes_no_outcomes(outcomes, tokens)
            return DiscoveredMarketBinding(
                market=market,
                outcomes=om,
                session_role=session_role,
                window_slug=str(event.get("slug") or slug),
                resolution_source=str(selected.get("resolutionSource") or "") or None,
                resolution_rule_fingerprint=rule_fingerprint(
                    resolution_source=str(selected.get("resolutionSource") or ""),
                    description=str(selected.get("description") or ""),
                ),
                market_rule_ok=True,
                requested_window_start=market.event_start,
            )
        if request.condition_id:
            url = f"https://gamma-api.polymarket.com/markets?condition_ids={request.condition_id}"
            markets = _http_get_json(url, opener=self._opener)
            if not markets:
                raise ValueError(f"no market for condition_id={request.condition_id}")
            m = markets[0]
            event = {"slug": m.get("slug"), "markets": [m], "title": m.get("question")}
            market = market_from_gamma_event(event, condition_id=request.condition_id)
            om = map_yes_no_outcomes(m.get("outcomes"), m.get("clobTokenIds"))
            return DiscoveredMarketBinding(
                market=market,
                outcomes=om,
                session_role=session_role,
                window_slug=str(m.get("slug") or ""),
                resolution_source=str(m.get("resolutionSource") or "") or None,
                resolution_rule_fingerprint=rule_fingerprint(
                    resolution_source=str(m.get("resolutionSource") or ""),
                    description=str(m.get("description") or ""),
                ),
                market_rule_ok=True,
                requested_window_start=market.event_start,
            )
        raise ValueError("GammaMarketDiscovery needs event_slug, event_url, or condition_id")

    async def resolve_btc_5m_window(
        self,
        *,
        window_start: datetime | None = None,
        slug: str | None = None,
        session_role: DiscoverySessionRole = DiscoverySessionRole.CANDIDATE,
    ) -> DiscoveredMarketBinding:
        """Deterministic slug construction + exact Gamma lookup + validation."""
        if slug is None:
            if window_start is None:
                raise ValueError("window_start or slug required")
            if window_start.tzinfo is None:
                window_start = window_start.replace(tzinfo=timezone.utc)
            epoch = int(window_start.timestamp())
            if epoch % BTC_5M_WINDOW_S != 0:
                raise ValueError("window_start must align to 300s boundary")
            slug = f"btc-updown-5m-{epoch}"
        return await self.resolve_binding(
            MarketRequest(event_slug=slug),
            session_role=session_role,
            require_btc_5m_rules=True,
        )

    async def prepare_next_btc_5m(
        self, *, now: datetime | None = None
    ) -> DiscoveredMarketBinding:
        """Discover the next window without activating it (N4 will promote)."""
        slug = next_btc_updown_slug(now)
        return await self.resolve_btc_5m_window(
            slug=slug, session_role=DiscoverySessionRole.PREPARED_NEXT
        )


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
