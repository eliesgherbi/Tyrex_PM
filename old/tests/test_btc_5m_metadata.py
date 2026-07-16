"""Tests for generic BTC 5m metadata resolver (A0.1)."""

from __future__ import annotations

import httpx
import pytest

from tyrex_pm.runtime.btc_5m_metadata import (
    Btc5mMarketMetadata,
    from_paired_binary_metadata,
    is_btc_5m_metadata_placeholder,
    merge_z_gap_metadata,
    resolve_and_apply_btc_5m_metadata,
    resolve_btc_5m_event_metadata,
    validate_btc_5m_metadata,
)
from tyrex_pm.runtime.config import parse_app_config
from tyrex_pm.venue.polymarket.event_metadata import EventMetadataError, PairedBinaryEventMetadata

SAMPLE_META = PairedBinaryEventMetadata(
    market_id="btc_5m_20260701_2045",
    condition_id="0xf3ebb689e1fb5cff05f1b5aa7e43380c2b9cc4300916f2a27b5ad76581bdbf48",
    yes_token_id="60623465405255973232915789757360817490886260321202505048709097730361810974033",
    no_token_id="39973642943404485458661593289415231607414175313101347321562244976405845796515",
    event_start_ts=1782938400.0,
    event_end_ts=1782938700.0,
    event_slug="btc-updown-5m-1782938400",
    event_title="Bitcoin Up or Down",
    yes_outcome_label="Up",
    no_outcome_label="Down",
    market_slug="btc-updown-5m-1782938400",
)

SAMPLE_EVENT = {
    "title": "Bitcoin Up or Down - July 1, 8:40PM-8:45PM ET",
    "slug": "btc-updown-5m-1782938400",
    "startTime": "2026-07-01T20:40:00Z",
    "endDate": "2026-07-01T20:45:00Z",
    "markets": [
        {
            "slug": "btc-updown-5m-1782938400",
            "conditionId": SAMPLE_META.condition_id,
            "clobTokenIds": [SAMPLE_META.yes_token_id, SAMPLE_META.no_token_id],
            "outcomes": ["Up", "Down"],
            "eventStartTime": "2026-07-01T20:40:00Z",
            "endDate": "2026-07-01T20:45:00Z",
            "enableOrderBook": True,
        }
    ],
}


def _z_gap_app(**over):
    zg = {
        "market_id": "btc_5m_<YYYYMMDD_HHMM>",
        "condition_id": "<required>",
        "event_start_ts": None,
        "event_end_ts": None,
        "yes_token_id": "<required>",
        "no_token_id": "<required>",
        **over,
    }
    return parse_app_config(
        risk={
            "notional": {"min_usd": "0.01", "max_usd": "5", "max_policy": "deny"},
            "deployment": {"token_cap_usd": "25", "portfolio_cap_usd": "100"},
            "venue_min_size": {"enabled": False},
            "capital": {"enabled": False, "max_wallet_age_s": 120},
            "concurrency": {"max_orders_in_flight": 4},
            "readiness": {
                "require_wallet_sync": False,
                "max_wallet_age_s_live": 120,
                "require_heartbeat_live": False,
                "require_user_ws_live": False,
            },
        },
        strategy={"kind": "z_gap", "enabled": True, "z_gap": zg},
        runtime={
            "execution_mode": "live",
            "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
            "market_data": {"enabled": True},
        },
    )


def test_btc_5m_metadata_maps_fields() -> None:
    meta = from_paired_binary_metadata(SAMPLE_META)
    assert meta.market_id == SAMPLE_META.market_id
    assert meta.condition_id == SAMPLE_META.condition_id
    assert meta.up_token_id == SAMPLE_META.yes_token_id
    assert meta.down_token_id == SAMPLE_META.no_token_id
    assert meta.event_start_ts == SAMPLE_META.event_start_ts
    assert meta.event_end_ts == SAMPLE_META.event_end_ts
    assert meta.event_slug == SAMPLE_META.event_slug


def test_missing_token_ids_fail_closed() -> None:
    meta = Btc5mMarketMetadata(
        market_id="btc_5m_20260701_2045",
        condition_id="0xabc",
        yes_token_id="<required>",
        no_token_id="222",
        event_start_ts=1.0,
        event_end_ts=2.0,
        event_slug="slug",
    )
    with pytest.raises(EventMetadataError, match="yes_token_id"):
        validate_btc_5m_metadata(meta)


def test_invalid_event_bounds_fail_closed() -> None:
    meta = Btc5mMarketMetadata(
        market_id="btc_5m_20260701_2045",
        condition_id="0xabc",
        yes_token_id="111",
        no_token_id="222",
        event_start_ts=100.0,
        event_end_ts=100.0,
        event_slug="slug",
    )
    with pytest.raises(EventMetadataError, match="event_end_ts"):
        validate_btc_5m_metadata(meta)


def test_merge_z_gap_metadata_fills_placeholders() -> None:
    app = _z_gap_app()
    assert app.z_gap is not None
    meta = from_paired_binary_metadata(SAMPLE_META)
    merged = merge_z_gap_metadata(app.z_gap, meta)
    assert merged.market_id == SAMPLE_META.market_id
    assert merged.yes_token_id == SAMPLE_META.yes_token_id
    assert merged.event_end_ts == SAMPLE_META.event_end_ts


def test_is_placeholder_detects_templates() -> None:
    assert is_btc_5m_metadata_placeholder("market_id", "btc_5m_<YYYYMMDD_HHMM>")
    assert is_btc_5m_metadata_placeholder("yes_token_id", "<required>")
    assert not is_btc_5m_metadata_placeholder("market_id", "btc_5m_20260701_2045")


def _mock_httpx_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("tyrex_pm.venue.polymarket.event_metadata.httpx.Client", factory)


def test_resolve_btc_5m_from_event_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events/slug/btc-updown-5m-1782938400"):
            return httpx.Response(200, json=SAMPLE_EVENT)
        return httpx.Response(404, json={"error": "not found"})

    _mock_httpx_client(monkeypatch, httpx.MockTransport(handler))
    meta = resolve_btc_5m_event_metadata(
        "https://polymarket.com/event/btc-updown-5m-1782938400"
    )
    assert meta.yes_outcome_label == "Up"
    assert meta.market_id == "btc_5m_20260701_2045"


def test_resolve_and_apply_btc_5m_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events/slug/btc-updown-5m-1782938400"):
            return httpx.Response(200, json=SAMPLE_EVENT)
        return httpx.Response(404, json={"error": "not found"})

    _mock_httpx_client(monkeypatch, httpx.MockTransport(handler))
    app = _z_gap_app()
    app, meta = resolve_and_apply_btc_5m_metadata(
        app,
        event_url="https://polymarket.com/event/btc-updown-5m-1782938400",
    )
    assert meta is not None
    assert app.z_gap is not None
    assert app.z_gap.condition_id == SAMPLE_META.condition_id
