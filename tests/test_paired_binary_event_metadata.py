"""Tests for Polymarket event metadata resolution and paired-binary overrides."""

from __future__ import annotations

import httpx
import pytest

from tyrex_pm.runtime.config import parse_app_config
from tyrex_pm.runtime.paired_binary_metadata import (
    apply_event_metadata_to_app,
    is_paired_binary_metadata_placeholder,
    merge_paired_binary_metadata,
    resolve_and_apply_paired_binary_metadata,
)
from tyrex_pm.venue.polymarket.event_metadata import (
    PairedBinaryEventMetadata,
    parse_event_ref,
    resolve_paired_binary_event_metadata,
)

SAMPLE_EVENT = {
    "title": "Bitcoin Up or Down - July 1, 8:40PM-8:45PM ET",
    "slug": "btc-updown-5m-1782938400",
    "startTime": "2026-07-01T20:40:00Z",
    "endDate": "2026-07-01T20:45:00Z",
    "markets": [
        {
            "slug": "btc-updown-5m-1782938400",
            "conditionId": "0xf3ebb689e1fb5cff05f1b5aa7e43380c2b9cc4300916f2a27b5ad76581bdbf48",
            "clobTokenIds": [
                "60623465405255973232915789757360817490886260321202505048709097730361810974033",
                "39973642943404485458661593289415231607414175313101347321562244976405845796515",
            ],
            "outcomes": ["Up", "Down"],
            "eventStartTime": "2026-07-01T20:40:00Z",
            "endDate": "2026-07-01T20:45:00Z",
            "enableOrderBook": True,
        }
    ],
}

SAMPLE_META = PairedBinaryEventMetadata(
    market_id="btc_5m_20260701_2045",
    condition_id="0xf3ebb689e1fb5cff05f1b5aa7e43380c2b9cc4300916f2a27b5ad76581bdbf48",
    yes_token_id="60623465405255973232915789757360817490886260321202505048709097730361810974033",
    no_token_id="39973642943404485458661593289415231607414175313101347321562244976405845796515",
    event_start_ts=1782938400.0,
    event_end_ts=1782938700.0,
    event_slug="btc-updown-5m-1782938400",
    event_title="Bitcoin Up or Down - July 1, 8:40PM-8:45PM ET",
    yes_outcome_label="Up",
    no_outcome_label="Down",
    market_slug="btc-updown-5m-1782938400",
)


def _paired_binary_cfg(**over):
    from paired_binary_shutdown_helpers import app_cfg, risk_cfg, strategy_cfg

    base = app_cfg(max_runtime_s=600)
    pb = {
        "market_id": "btc_5m_<YYYYMMDD_HHMM>",
        "condition_id": "<required>",
        "event_start_ts": None,
        "event_end_ts": None,
        "yes_token_id": "<required>",
        "no_token_id": "<required>",
        "use_fixture_book": False,
        **over,
    }
    return parse_app_config(
        risk=risk_cfg(),
        strategy=strategy_cfg(**pb),
        runtime={
            "execution_mode": "live",
            "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
            "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
            "market_data": {"enabled": True},
            "execution": {"planner": {"enabled": True}},
            "strategy_lifecycle": {"mode": "market_aware", "max_runtime_s": None},
            "survival": {"enabled": True},
        },
    )


def test_parse_event_ref_accepts_polymarket_url() -> None:
    ref = parse_event_ref("https://polymarket.com/fr/event/btc-updown-5m-1782938400")
    assert ref.kind == "event"
    assert ref.slug == "btc-updown-5m-1782938400"


def test_is_placeholder_detects_template_values() -> None:
    assert is_paired_binary_metadata_placeholder("market_id", "btc_5m_<YYYYMMDD_HHMM>")
    assert is_paired_binary_metadata_placeholder("yes_token_id", "<required>")
    assert not is_paired_binary_metadata_placeholder("market_id", "btc_5m_20260701_2045")


def test_merge_metadata_fills_placeholders_only() -> None:
    app = _paired_binary_cfg()
    assert app.paired_binary is not None
    merged = merge_paired_binary_metadata(app.paired_binary, SAMPLE_META)
    assert merged.market_id == SAMPLE_META.market_id
    assert merged.condition_id == SAMPLE_META.condition_id
    assert merged.yes_token_id == SAMPLE_META.yes_token_id
    assert merged.event_end_ts == SAMPLE_META.event_end_ts


def test_merge_metadata_keeps_explicit_config_values() -> None:
    app = _paired_binary_cfg(
        market_id="btc_5m_20260701_2045",
        yes_token_id="111",
        no_token_id="222",
    )
    assert app.paired_binary is not None
    merged = merge_paired_binary_metadata(app.paired_binary, SAMPLE_META)
    assert merged.market_id == "btc_5m_20260701_2045"
    assert merged.yes_token_id == "111"
    assert merged.no_token_id == "222"
    assert merged.condition_id == SAMPLE_META.condition_id
    assert merged.event_end_ts == SAMPLE_META.event_end_ts


def _mock_httpx_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("tyrex_pm.venue.polymarket.event_metadata.httpx.Client", factory)


def test_resolve_event_metadata_from_gamma(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events/slug/btc-updown-5m-1782938400"):
            return httpx.Response(200, json=SAMPLE_EVENT)
        return httpx.Response(404, json={"error": "not found"})

    _mock_httpx_client(monkeypatch, httpx.MockTransport(handler))
    meta = resolve_paired_binary_event_metadata(
        "https://polymarket.com/event/btc-updown-5m-1782938400"
    )
    assert meta.yes_outcome_label == "Up"
    assert meta.no_outcome_label == "Down"
    assert meta.market_id == "btc_5m_20260701_2045"


def test_resolve_and_apply_without_event_url_and_placeholders() -> None:
    app = _paired_binary_cfg()
    app, meta = resolve_and_apply_paired_binary_metadata(app, event_url=None)
    assert meta is None
    assert app.paired_binary is not None
    assert app.paired_binary.yes_token_id == "<required>"


def test_resolve_and_apply_from_event_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events/slug/btc-updown-5m-1782938400"):
            return httpx.Response(200, json=SAMPLE_EVENT)
        return httpx.Response(404, json={"error": "not found"})

    _mock_httpx_client(monkeypatch, httpx.MockTransport(handler))
    app = _paired_binary_cfg()
    app, meta = resolve_and_apply_paired_binary_metadata(
        app,
        event_url="https://polymarket.com/event/btc-updown-5m-1782938400",
    )
    assert meta is not None
    assert app.paired_binary is not None
    assert app.paired_binary.market_id == SAMPLE_META.market_id
    assert app.paired_binary.yes_token_id == SAMPLE_META.yes_token_id


def test_resolve_and_apply_from_real_tokens_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/markets") and "clob_token_ids" in str(request.url):
            return httpx.Response(200, json=[SAMPLE_EVENT["markets"][0]])
        return httpx.Response(404, json={"error": "not found"})

    _mock_httpx_client(monkeypatch, httpx.MockTransport(handler))
    app = _paired_binary_cfg(
        yes_token_id=SAMPLE_META.yes_token_id,
        no_token_id=SAMPLE_META.no_token_id,
    )
    app, meta = resolve_and_apply_paired_binary_metadata(app, event_url=None)
    assert meta is not None
    assert app.paired_binary is not None
    assert app.paired_binary.condition_id == SAMPLE_META.condition_id
    assert app.paired_binary.event_end_ts == SAMPLE_META.event_end_ts


def test_apply_event_metadata_to_app() -> None:
    app = _paired_binary_cfg()
    updated = apply_event_metadata_to_app(app, SAMPLE_META)
    pb = updated.paired_binary
    assert pb is not None
    assert pb.market_id == SAMPLE_META.market_id
    assert pb.condition_id == SAMPLE_META.condition_id
