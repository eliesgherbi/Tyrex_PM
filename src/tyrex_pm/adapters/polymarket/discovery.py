"""Market discovery: fixture JSON and official polymarket-client Gamma surface."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
    OutcomeMapError,
    map_up_down_outcomes,
    map_yes_no_outcomes,
    rule_fingerprint,
)

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CHAINLINK_BTC_USD_STREAM = "https://data.chain.link/streams/btc-usd"
_BTC_5M_SLUG = re.compile(r"^btc-updown-5m-(\d{10,})$", re.IGNORECASE)

MappingLike = dict[str, Any]


def sdk_event_to_gamma_dict(event: Any) -> dict[str, Any]:
    """Translate official SDK Event model into the legacy Gamma JSON shape.

    Downstream binders still expect ``clobTokenIds`` + label ``outcomes`` lists.

    Timestamp semantics (verified against polymarket-client 0.2.0 Event.schedule):
    - ``schedule.start_time`` — recurring resolution window start (matches slug epoch)
    - ``schedule.start_date`` / ``market.state.start_date`` — listing/publication time
      (often ~1 day earlier); NOT the five-minute boundary
    - ``schedule.end_date`` / ``market.state.end_date`` — window end
    """
    if isinstance(event, Mapping):
        raw = dict(event)
    else:
        raw = event.model_dump(mode="json", by_alias=True)
    schedule = raw.get("schedule") if isinstance(raw.get("schedule"), dict) else {}
    # Authoritative schedule window start when present (equals slug epoch for BTC 5m).
    schedule_window_start = schedule.get("start_time") or schedule.get("startTime")
    schedule_listed_at = schedule.get("start_date") or schedule.get("startDate")
    schedule_end = (
        schedule.get("end_date")
        or schedule.get("endDate")
        or schedule.get("end_time")
        or schedule.get("endTime")
    )
    markets_out: list[dict[str, Any]] = []
    for m in raw.get("markets") or []:
        if not isinstance(m, dict):
            continue
        outcomes_obj = m.get("outcomes") or {}
        labels: list[str] = []
        tokens: list[str] = []
        if isinstance(outcomes_obj, dict):
            # Preserve Yes/No key order when present; else insertion order.
            for key in ("yes", "no"):
                row = outcomes_obj.get(key)
                if isinstance(row, dict):
                    labels.append(str(row.get("label") or key))
                    tokens.append(str(row.get("token_id") or ""))
            if not labels:
                for key, row in outcomes_obj.items():
                    if isinstance(row, dict):
                        labels.append(str(row.get("label") or key))
                        tokens.append(str(row.get("token_id") or ""))
        elif isinstance(outcomes_obj, list):
            labels = [str(x) for x in outcomes_obj]
        trading = m.get("trading") or {}
        state = m.get("state") or {}
        resolution = m.get("resolution") or raw.get("resolution") or {}
        fee_schedule = trading.get("fee_schedule") if isinstance(trading, dict) else None
        fd = None
        if isinstance(fee_schedule, dict):
            fd = {
                "r": fee_schedule.get("rate"),
                "e": fee_schedule.get("exponent"),
                "to": fee_schedule.get("taker_only", True),
            }
        listed_at = state.get("start_date") or state.get("startDate") or schedule_listed_at
        markets_out.append(
            {
                "conditionId": m.get("condition_id") or m.get("conditionId"),
                "question": m.get("question"),
                "description": m.get("description") or raw.get("description"),
                "outcomes": labels,
                "clobTokenIds": tokens,
                "orderPriceMinTickSize": trading.get("minimum_tick_size"),
                "orderMinSize": trading.get("minimum_order_size"),
                # Do NOT map state.start_date → eventStartTime (listing ≠ window).
                "listedAt": listed_at,
                "endDate": state.get("end_date") or state.get("endDate") or schedule_end,
                "active": state.get("active"),
                "closed": state.get("closed"),
                "accepting_orders": state.get("accepting_orders"),
                "fd": fd,
                "resolutionSource": (
                    resolution.get("source") if isinstance(resolution, dict) else None
                ),
            }
        )
    return {
        "slug": raw.get("slug"),
        "title": raw.get("title"),
        "description": raw.get("description"),
        "startTime": schedule_window_start or raw.get("startTime"),
        "listedAt": schedule_listed_at,
        "createdAt": raw.get("created_at") or raw.get("createdAt"),
        "endDate": schedule_end or raw.get("endDate"),
        "resolutionSource": (raw.get("resolution") or {}).get("source")
        if isinstance(raw.get("resolution"), dict)
        else raw.get("resolutionSource"),
        "markets": markets_out,
    }


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text).astimezone(timezone.utc)


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
    yes, no = make_binary_instruments(market_id=market_id, yes_token=yes_token, no_token=no_token)
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


def _select_market(event: MappingLike, *, condition_id: str | None = None) -> MappingLike:
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
    """Build BinaryMarket with YES←Up and NO←Down instrument slots (legacy fields).

    Prefer ``event.startTime`` (SDK schedule window) over legacy ``eventStartTime``,
    which may carry listing/publication time depending on payload shape.
    BTC 5m binders overwrite start/end with the slug-derived canonical window.
    """
    cid_raw = selected.get("conditionId")
    if cid_raw is None:
        cid_raw = selected.get("condition_id")
    cid = str(cid_raw).strip() if cid_raw is not None else ""
    if not cid:
        raise ValueError("MARKET_IDENTIFIERS_MISSING: missing conditionId")
    market_id = MarketId(cid)
    yes, no = make_binary_instruments(
        market_id=market_id,
        yes_token=up_token,
        no_token=down_token,
    )
    tick = (
        selected.get("orderPriceMinTickSize")
        or selected.get("minimum_tick_size")
        or selected.get("tick_size")
        or (selected.get("trading") or {}).get("minimum_tick_size")
        or (selected.get("trading") or {}).get("orderPriceMinTickSize")
    )
    min_size = (
        selected.get("orderMinSize")
        or selected.get("minimum_order_size")
        or selected.get("min_order_size")
        or (selected.get("trading") or {}).get("minimum_order_size")
    )
    return BinaryMarket(
        market_id=market_id,
        condition_id=cid,
        question=str(selected.get("question") or event.get("title") or ""),
        yes=yes,
        no=no,
        event_start=_parse_dt(event.get("startTime") or selected.get("eventStartTime")),
        event_end=_parse_dt(selected.get("endDate") or event.get("endDate")),
        tick_size=None if tick is None else Decimal(str(tick)),
        min_order_size=None if min_size is None else Decimal(str(min_size)),
        status=_status(selected),
        event_slug=str(event.get("slug") or ""),
    )


def market_from_gamma_event(event: MappingLike, *, condition_id: str | None = None) -> BinaryMarket:
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
            return _binary_market_from_selected(event, selected, up_token=up_t, down_token=down_t)
        if "yes" in lowered and "no" in lowered:
            om = map_yes_no_outcomes(outcomes, token_ids)
            yes_t, no_t = om.as_yes_no()
            return _binary_market_from_selected(event, selected, up_token=yes_t, down_token=no_t)
    except OutcomeMapError as exc:
        raise ValueError(str(exc)) from exc
    raise ValueError(
        "outcome_map_rejected: positional mapping forbidden; need label-based Up/Down or Yes/No"
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

    Canonical window identity for this market family is the validated slug epoch
    (``window_start``) and ``window_start + 300s`` (``window_end``).

    Listing / publication timestamps (``listedAt``, ``startDate``, and legacy
    ``eventStartTime`` when it carries listing time) are never used as the
    five-minute boundary. Optional ``event.startTime`` (SDK ``schedule.start_time``)
    is cross-checked when present because it shares recurring-window semantics.
    """
    slug = str(event.get("slug") or "").strip()
    if expected_slug and not slug:
        raise ValueError(
            "wrong_window:STALE_DISCOVERY_PAYLOAD: empty returned slug "
            f"for expected={expected_slug!r}"
        )
    if expected_slug and slug != expected_slug:
        raise ValueError(
            f"wrong_window:MARKET_SLUG_MISMATCH: returned slug={slug!r} expected={expected_slug!r}"
        )
    if not _BTC_5M_SLUG.match(slug):
        raise ValueError(f"wrong_window:MARKET_SLUG_MALFORMED: not a btc-updown-5m slug: {slug!r}")

    try:
        slug_epoch = parse_slug_epoch(slug)
    except Exception as exc:
        raise ValueError(
            f"wrong_window:MARKET_SLUG_MALFORMED: missing/invalid epoch in {slug!r}"
        ) from exc
    if slug_epoch % BTC_5M_WINDOW_S != 0:
        raise ValueError(
            f"wrong_window:MARKET_WINDOW_ALIGNMENT_INVALID: slug epoch {slug_epoch} "
            f"not aligned to {BTC_5M_WINDOW_S}s"
        )
    start, end = window_from_slug_epoch(slug, slug_epoch)
    if expected_window_start is not None:
        if abs((start - expected_window_start).total_seconds()) >= 1.0:
            raise ValueError(
                f"wrong_window:MARKET_WINDOW_ALIGNMENT_INVALID: slug epoch "
                f"{start.isoformat()} != requested {expected_window_start.isoformat()}"
            )

    selected = _select_market(event, condition_id=condition_id)
    outcomes = selected.get("outcomes")
    token_ids = selected.get("clobTokenIds") or selected.get("clob_token_ids")
    if outcomes is None or token_ids is None:
        raise ValueError("MARKET_IDENTIFIERS_MISSING: missing outcomes or clobTokenIds")
    try:
        om = map_up_down_outcomes(outcomes, token_ids)
    except OutcomeMapError as exc:
        raise ValueError(f"MARKET_OUTCOME_BINDING_INVALID:{exc}") from exc

    up_t, down_t = om.as_up_down()
    if not up_t.value or not down_t.value:
        raise ValueError("MARKET_IDENTIFIERS_MISSING: empty Up/Down token id")
    market = _binary_market_from_selected(event, selected, up_token=up_t, down_token=down_t)

    # Authoritative schedule window start (SDK schedule.start_time), when present.
    schedule_start = _parse_dt(event.get("startTime"))
    if schedule_start is not None:
        if abs((schedule_start - start).total_seconds()) >= 1.0:
            raise ValueError(
                f"wrong_window:MARKET_WINDOW_ALIGNMENT_INVALID: schedule startTime "
                f"{schedule_start.isoformat()} != slug epoch {start.isoformat()}"
            )

    gamma_end = market.event_end
    if gamma_end is not None:
        if abs((gamma_end - end).total_seconds()) >= 1.0:
            raise ValueError(
                f"wrong_window:MARKET_WINDOW_END_MISMATCH: endDate "
                f"{gamma_end.isoformat()} != expected {end.isoformat()}"
            )

    # Canonical slug-derived window on the binding's market copy.
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
            raise ValueError(f"market_rule_source_mismatch: resolutionSource={resolution_source!r}")
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


def _fetch_gamma_event_via_sdk(slug: str, *, public_client: Any | None = None) -> dict[str, Any]:
    """Fetch event by slug through official PublicClient (no urllib)."""
    from tyrex_pm.adapters.polymarket.sdk_public import build_public_client

    owns = public_client is None
    client = public_client or build_public_client()
    try:
        event = client.get_event(slug=slug)
        return sdk_event_to_gamma_dict(event)
    finally:
        if owns and hasattr(client, "close"):
            try:
                client.close()
            except Exception:
                pass


def fetch_gamma_event(slug: str, *, public_client: Any | None = None) -> dict[str, Any]:
    """Public discovery entrypoint used by runtimes (SDK only)."""
    return _fetch_gamma_event_via_sdk(slug, public_client=public_client)


def market_info_from_gamma_market(m: MappingLike, *, condition_id: str) -> dict[str, Any]:
    """Build clob-markets-compatible dict from SDK-normalized gamma market row."""
    return {
        "ao": m.get("accepting_orders", True),
        "mts": m.get("orderPriceMinTickSize") or m.get("minimum_tick_size") or "0.01",
        "mos": m.get("orderMinSize") or m.get("minimum_order_size") or "5",
        "fd": m.get("fd"),
        "condition_id": condition_id,
    }


def book_asks_via_sdk(token_id: str) -> list[dict[str, str]]:
    from tyrex_pm.adapters.polymarket.rest_book import fetch_clob_book

    payload = fetch_clob_book(token_id)
    return [{"price": str(lv.price), "size": str(lv.quantity)} for lv in payload.book.asks]


class GammaMarketDiscovery:
    """Public Gamma discovery via official polymarket-client PublicClient."""

    def __init__(
        self,
        *,
        base_url: str = GAMMA_EVENTS_URL,
        opener: Any = None,
        public_client: Any | None = None,
        event_fetcher: Any | None = None,
    ) -> None:
        # base_url/opener retained for test call-site compatibility; unused in production.
        self._base_url = base_url
        self._opener = opener
        self._public_client = public_client
        self._event_fetcher = event_fetcher

    async def resolve_market(self, request: MarketRequest) -> BinaryMarket:
        binding = await self.resolve_binding(request, session_role=DiscoverySessionRole.ACTIVE)
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
            if self._event_fetcher is not None:
                event = self._event_fetcher(slug)
            elif self._opener is not None:
                # Legacy injectable opener used only by older tests; convert raw JSON.
                import json as _json
                from urllib.request import Request, urlopen

                opener = self._opener if self._opener is not urlopen else urlopen
                url = f"{self._base_url}?slug={slug}"
                req = Request(
                    url,
                    headers={
                        "User-Agent": "tyrex-pm/0.3 (read-only observe)",
                        "Accept": "application/json",
                    },
                )
                with opener(req, timeout=30.0) as resp:
                    data = _json.loads(resp.read().decode("utf-8"))
                event = data[0] if isinstance(data, list) else data
            else:
                event = _fetch_gamma_event_via_sdk(slug, public_client=self._public_client)
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
                for o in (outcomes if not isinstance(outcomes, str) else json.loads(outcomes))
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
            from tyrex_pm.adapters.polymarket.sdk_public import build_public_client

            client = self._public_client or build_public_client()
            owns = self._public_client is None
            try:
                items = list(
                    client.list_markets(
                        condition_ids=request.condition_id, page_size=5
                    ).iter_items()
                )
            finally:
                if owns and hasattr(client, "close"):
                    try:
                        client.close()
                    except Exception:
                        pass
            if not items:
                raise ValueError(f"no market for condition_id={request.condition_id}")
            event = sdk_event_to_gamma_dict(
                {
                    "slug": getattr(items[0], "slug", None),
                    "title": getattr(items[0], "question", None),
                    "description": getattr(items[0], "description", None),
                    "resolution": getattr(items[0], "resolution", None),
                    "markets": [items[0].model_dump(mode="json", by_alias=True)],
                }
            )
            m = (event.get("markets") or [None])[0]
            if not m:
                raise ValueError(f"no market for condition_id={request.condition_id}")
            market = market_from_gamma_event(event, condition_id=request.condition_id)
            om = map_yes_no_outcomes(m.get("outcomes"), m.get("clobTokenIds"))
            return DiscoveredMarketBinding(
                market=market,
                outcomes=om,
                session_role=session_role,
                window_slug=str(m.get("slug") or event.get("slug") or ""),
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

    async def prepare_next_btc_5m(self, *, now: datetime | None = None) -> DiscoveredMarketBinding:
        """Discover the next window without activating it."""
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
