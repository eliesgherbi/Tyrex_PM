"""BTC 5m market-window identity: listing timestamps ≠ resolution boundary."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tyrex_pm.adapters.polymarket.btc_5m_window import (
    BTC_5M_WINDOW_S,
    MarketWindowError,
    resolve_btc_5m_window,
)
from tyrex_pm.adapters.polymarket.discovery import (
    bind_btc_5m_gamma_event,
    sdk_event_to_gamma_dict,
)
from tyrex_pm.adapters.polymarket.sdk_errors import (
    primary_blocker_from_compose_errors,
    vpn_hint_from_error_texts,
)
from tyrex_pm.domain.polymarket.discovery_binding import DiscoverySessionRole
from tyrex_pm.domain.polymarket.ptb_capture import PtbCaptureEngine
from tyrex_pm.runtime.n7_ptb_policy import ptb_trust_fields

PRODUCTION_SLUG = "btc-updown-5m-1785495000"
PRODUCTION_START = datetime(2026, 7, 31, 10, 50, tzinfo=timezone.utc)
PRODUCTION_END = datetime(2026, 7, 31, 10, 55, tzinfo=timezone.utc)
PRODUCTION_LISTED = "2026-07-30T10:58:31.450519+00:00"
CHAINLINK = "https://data.chain.link/streams/btc-usd"
UP_TOKEN = "11111111111111111111111111111111111111111111111111111111111111111111111111111"
DOWN_TOKEN = "22222222222222222222222222222222222222222222222222222222222222222222222222222"
CONDITION = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _production_shaped_gamma(
    *,
    slug: str = PRODUCTION_SLUG,
    listed_at: str = PRODUCTION_LISTED,
    end_date: str = "2026-07-31T10:55:00+00:00",
    start_time: str | None = "2026-07-31T10:50:00+00:00",
    event_start_time: str | None = PRODUCTION_LISTED,
    outcomes: list[str] | None = None,
    tokens: list[str] | None = None,
    condition_id: str = CONDITION,
    include_end: bool = True,
) -> dict:
    """Fixture mirroring production: listing day-before, slug epoch = window start."""
    market: dict = {
        "conditionId": condition_id,
        "question": "Bitcoin Up or Down - July 31, 6:50AM-6:55AM ET",
        "description": f"Chainlink BTC/USD at {CHAINLINK}",
        "outcomes": outcomes or ["Up", "Down"],
        "clobTokenIds": tokens or [UP_TOKEN, DOWN_TOKEN],
        "resolutionSource": CHAINLINK,
        "active": True,
        "closed": False,
        "listedAt": listed_at,
        "orderPriceMinTickSize": 0.01,
        "orderMinSize": 5,
    }
    if event_start_time is not None:
        # Ambiguous legacy field carrying listing time (the failed-run shape).
        market["eventStartTime"] = event_start_time
    if include_end:
        market["endDate"] = end_date
    event: dict = {
        "slug": slug,
        "title": market["question"],
        "description": market["description"],
        "listedAt": listed_at,
        "createdAt": "2026-07-30T10:58:00+00:00",
        "markets": [market],
        "resolutionSource": CHAINLINK,
    }
    if start_time is not None:
        event["startTime"] = start_time
    if include_end:
        event["endDate"] = end_date
    return event


def _sdk_event_dump_production() -> dict:
    """SDK Event.model_dump-shaped payload (schedule.start_date = listing)."""
    return {
        "slug": PRODUCTION_SLUG,
        "title": "Bitcoin Up or Down - July 31, 6:50AM-6:55AM ET",
        "description": f"Chainlink at {CHAINLINK}",
        "created_at": "2026-07-30T10:58:00+00:00",
        "schedule": {
            "start_time": "2026-07-31T10:50:00+00:00",
            "start_date": PRODUCTION_LISTED,
            "end_date": "2026-07-31T10:55:00+00:00",
        },
        "resolution": {"source": CHAINLINK},
        "markets": [
            {
                "condition_id": CONDITION,
                "question": "Bitcoin Up or Down - July 31, 6:50AM-6:55AM ET",
                "description": f"Chainlink at {CHAINLINK}",
                "outcomes": {
                    "yes": {"label": "Up", "token_id": UP_TOKEN},
                    "no": {"label": "Down", "token_id": DOWN_TOKEN},
                },
                "trading": {
                    "minimum_tick_size": "0.01",
                    "minimum_order_size": "5",
                },
                "state": {
                    "start_date": PRODUCTION_LISTED,
                    "end_date": "2026-07-31T10:55:00+00:00",
                    "active": True,
                    "closed": False,
                    "accepting_orders": True,
                },
                "resolution": {"source": CHAINLINK},
            }
        ],
    }


def test_accept_when_event_start_time_predates_window() -> None:
    event = _production_shaped_gamma()
    binding = bind_btc_5m_gamma_event(
        event,
        expected_slug=PRODUCTION_SLUG,
        session_role=DiscoverySessionRole.ACTIVE,
    )
    assert binding.market.event_start == PRODUCTION_START
    assert binding.market.event_end == PRODUCTION_END
    assert (binding.market.event_end - binding.market.event_start).total_seconds() == 300


def test_event_start_time_not_exposed_as_window_start() -> None:
    gamma = sdk_event_to_gamma_dict(_sdk_event_dump_production())
    assert gamma["listedAt"] == PRODUCTION_LISTED
    assert gamma["startTime"] == "2026-07-31T10:50:00+00:00"
    m0 = gamma["markets"][0]
    assert m0.get("listedAt") == PRODUCTION_LISTED
    assert m0.get("eventStartTime") is None  # must not carry listing as eventStartTime
    binding = bind_btc_5m_gamma_event(gamma, expected_slug=PRODUCTION_SLUG)
    assert binding.market.event_start == PRODUCTION_START
    listed = datetime.fromisoformat(PRODUCTION_LISTED)
    assert binding.market.event_start != listed
    assert gamma["listedAt"] == PRODUCTION_LISTED


def test_canonical_window_from_slug_epoch() -> None:
    binding = bind_btc_5m_gamma_event(
        _production_shaped_gamma(event_start_time=PRODUCTION_LISTED),
        expected_slug=PRODUCTION_SLUG,
    )
    assert binding.market.event_start == PRODUCTION_START
    assert binding.market.event_end == PRODUCTION_END
    assert binding.window_slug == PRODUCTION_SLUG
    assert binding.requested_window_start == PRODUCTION_START


def test_resolve_btc_5m_window_ignores_listing_event_start_time() -> None:
    w = resolve_btc_5m_window(
        slug=PRODUCTION_SLUG,
        event={
            "startTime": "2026-07-31T10:50:00+00:00",
            "listedAt": PRODUCTION_LISTED,
            "endDate": "2026-07-31T10:55:00+00:00",
            "title": "Bitcoin Up or Down - July 31, 6:50AM-6:55AM ET",
        },
        market={
            "eventStartTime": PRODUCTION_LISTED,  # listing — must not reject
            "listedAt": PRODUCTION_LISTED,
            "endDate": "2026-07-31T10:55:00+00:00",
            "acceptingOrders": True,
            "question": "Bitcoin Up or Down - July 31, 6:50AM-6:55AM ET",
        },
    )
    assert w.market_start == PRODUCTION_START
    assert w.market_end == PRODUCTION_END
    assert w.listed_at is not None
    assert w.listed_at.date().isoformat() == "2026-07-30"
    assert w.event_start_time is not None
    assert w.event_start_time != w.market_start


def test_ptb_and_strategy_receive_canonical_boundary() -> None:
    binding = bind_btc_5m_gamma_event(
        _production_shaped_gamma(),
        expected_slug=PRODUCTION_SLUG,
        session_role=DiscoverySessionRole.ACTIVE,
    )
    # N4ObserveRuntime.open_session → ptb_engine.open_window(event_start=market.event_start)
    engine = PtbCaptureEngine()
    engine.open_window(
        market_id=binding.market.market_id,
        window_id=binding.window_slug,
        event_start=binding.market.event_start,
        event_end=binding.market.event_end,
    )
    state = engine.get_window(binding.market.market_id, binding.window_slug)
    assert state is not None
    assert state.event_start == PRODUCTION_START
    assert binding.requested_window_start == PRODUCTION_START
    assert binding.market.event_end == PRODUCTION_END


def test_reject_different_returned_slug() -> None:
    event = _production_shaped_gamma(slug="btc-updown-5m-1785495300")
    with pytest.raises(ValueError, match="MARKET_SLUG_MISMATCH"):
        bind_btc_5m_gamma_event(event, expected_slug=PRODUCTION_SLUG)


def test_reject_stale_cached_payload() -> None:
    event = _production_shaped_gamma(slug="")
    with pytest.raises(ValueError, match="STALE_DISCOVERY_PAYLOAD"):
        bind_btc_5m_gamma_event(event, expected_slug=PRODUCTION_SLUG)


def test_reject_malformed_slug() -> None:
    event = _production_shaped_gamma(slug="btc-updown-1h-1785495000")
    with pytest.raises(ValueError, match="MARKET_SLUG_MALFORMED"):
        bind_btc_5m_gamma_event(event, expected_slug="btc-updown-1h-1785495000")


def test_reject_non_aligned_epoch() -> None:
    bad_slug = "btc-updown-5m-1785495001"  # not divisible by 300
    event = _production_shaped_gamma(
        slug=bad_slug,
        start_time=None,
        end_date="2026-07-31T10:55:01+00:00",
        include_end=False,
    )
    # end omitted so end mismatch does not fire first
    event["markets"][0].pop("endDate", None)
    with pytest.raises(ValueError, match="MARKET_WINDOW_ALIGNMENT_INVALID"):
        bind_btc_5m_gamma_event(event, expected_slug=bad_slug)


def test_reject_authoritative_end_mismatch() -> None:
    event = _production_shaped_gamma(end_date="2026-07-31T11:00:00+00:00")
    with pytest.raises(ValueError, match="MARKET_WINDOW_END_MISMATCH"):
        bind_btc_5m_gamma_event(event, expected_slug=PRODUCTION_SLUG)


def test_reject_missing_condition_and_tokens() -> None:
    event = _production_shaped_gamma(condition_id="")
    event["markets"][0]["conditionId"] = ""
    with pytest.raises(ValueError, match="MARKET_IDENTIFIERS_MISSING"):
        bind_btc_5m_gamma_event(event, expected_slug=PRODUCTION_SLUG)
    event2 = _production_shaped_gamma()
    del event2["markets"][0]["clobTokenIds"]
    with pytest.raises(ValueError, match="MARKET_IDENTIFIERS_MISSING"):
        bind_btc_5m_gamma_event(event2, expected_slug=PRODUCTION_SLUG)


def test_reject_bad_outcomes() -> None:
    with pytest.raises(ValueError, match="MARKET_OUTCOME_BINDING_INVALID"):
        bind_btc_5m_gamma_event(
            _production_shaped_gamma(outcomes=["Up", "Up"], tokens=[UP_TOKEN, DOWN_TOKEN]),
            expected_slug=PRODUCTION_SLUG,
        )
    with pytest.raises(ValueError, match="MARKET_OUTCOME_BINDING_INVALID"):
        bind_btc_5m_gamma_event(
            _production_shaped_gamma(outcomes=["Up", "Sideways"], tokens=[UP_TOKEN, DOWN_TOKEN]),
            expected_slug=PRODUCTION_SLUG,
        )


def test_reversed_api_ordering_label_binding() -> None:
    binding = bind_btc_5m_gamma_event(
        _production_shaped_gamma(outcomes=["Down", "Up"], tokens=[DOWN_TOKEN, UP_TOKEN]),
        expected_slug=PRODUCTION_SLUG,
    )
    assert binding.up_token_id == UP_TOKEN
    assert binding.down_token_id == DOWN_TOKEN


def test_discovery_failure_preserves_primary_not_only_no_ptb_seal() -> None:
    errors = [
        "discovery:ValueError:wrong_window:MARKET_WINDOW_END_MISMATCH: "
        "endDate 2026-07-31T11:00:00+00:00 != expected 2026-07-31T10:55:00+00:00"
    ]
    primary, downstream = primary_blocker_from_compose_errors(errors)
    assert primary == "MARKET_WINDOW_END_MISMATCH"
    assert downstream == "PTB_NOT_STARTED"
    assert vpn_hint_from_error_texts(errors) is False


def test_market_identity_never_vpn_hint() -> None:
    assert (
        vpn_hint_from_error_texts(
            [
                "discovery:ValueError:wrong_window:MARKET_SLUG_MISMATCH: "
                "returned slug='x' expected='y'"
            ]
        )
        is False
    )
    assert vpn_hint_from_error_texts(["PTB_NOT_STARTED", "no_ptb_seal"]) is False


def test_transport_errors_still_vpn_hint() -> None:
    assert (
        vpn_hint_from_error_texts(
            ["sdk_transport:TransportError:connection refused to clob.polymarket.com"]
        )
        is True
    )
    assert (
        vpn_hint_from_error_texts(
            ["public_clob_unreachable: getaddrinfo failed / DNS"]
        )
        is True
    )
    assert (
        vpn_hint_from_error_texts(
            ["ssl.SSLCertVerificationError: certificate verify failed"]
        )
        is True
    )


def test_resolve_rejects_schedule_start_mismatch() -> None:
    with pytest.raises(MarketWindowError, match="MARKET_WINDOW_ALIGNMENT_INVALID"):
        resolve_btc_5m_window(
            slug=PRODUCTION_SLUG,
            event={
                "startTime": "2026-07-31T10:45:00+00:00",
                "endDate": "2026-07-31T10:55:00+00:00",
            },
            market={
                "endDate": "2026-07-31T10:55:00+00:00",
                "acceptingOrders": True,
            },
        )


def test_window_duration_constant() -> None:
    assert BTC_5M_WINDOW_S == 300


def test_ptb_trust_fields_still_compose() -> None:
    fields = ptb_trust_fields(sealed_k=None, require_ssr_price_match=True, ptb_ready=False)
    assert fields["ptb_ready"] is False
