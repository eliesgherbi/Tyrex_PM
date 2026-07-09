"""Market discovery tests (M2B.1-B)."""

from __future__ import annotations

import time

import pytest

from tyrex_pm.ingestion.market_discovery import (
    ELIGIBLE,
    EXPIRED,
    TOO_EARLY,
    MarketDiscoveryResult,
    classify_market_recording_eligibility,
    discover_btc_5m_by_slug,
    is_btc_5m_event_slug,
    parse_btc_5m_discovery_from_event,
    btc_5m_window_start_timestamps,
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

NON_BTC_EVENT = {
    "slug": "will-it-rain-tomorrow",
    "markets": [
        {
            "slug": "will-it-rain-tomorrow",
            "conditionId": "0xabc",
            "clobTokenIds": ["1", "2"],
            "outcomes": ["Yes", "No"],
            "eventStartTime": "2026-07-01T20:40:00Z",
            "endDate": "2026-07-01T20:45:00Z",
            "enableOrderBook": True,
        }
    ],
}


def test_is_btc_5m_event_slug() -> None:
    assert is_btc_5m_event_slug("btc-updown-5m-1782938400")
    assert not is_btc_5m_event_slug("will-it-rain-tomorrow")


def test_parse_btc_5m_discovery_from_event() -> None:
    result = parse_btc_5m_discovery_from_event(SAMPLE_EVENT)
    assert result is not None
    assert result.market_id == "btc_5m_20260701_2045"
    assert result.yes_token_id == SAMPLE_EVENT["markets"][0]["clobTokenIds"][0]
    assert result.no_token_id == SAMPLE_EVENT["markets"][0]["clobTokenIds"][1]
    assert result.event_slug == "btc-updown-5m-1782938400"


def test_non_btc_event_ignored() -> None:
    assert parse_btc_5m_discovery_from_event(NON_BTC_EVENT) is None


def test_btc_5m_window_start_timestamps_align_to_300s() -> None:
    starts = btc_5m_window_start_timestamps(1782938550, lookback_windows=0, lookahead_windows=0)
    assert starts == [1782938400]


def test_discover_btc_5m_by_slug_uses_gamma(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tyrex_pm.ingestion.market_discovery.fetch_btc_5m_event_by_slug",
        lambda slug, **kwargs: SAMPLE_EVENT,
    )
    result = discover_btc_5m_by_slug("btc-updown-5m-1782938400")
    assert isinstance(result, MarketDiscoveryResult)
    assert result.market_id == "btc_5m_20260701_2045"


def _sample_result() -> MarketDiscoveryResult:
    return MarketDiscoveryResult(
        market_id="btc_5m_20260701_2045",
        condition_id="0xabc",
        yes_token_id="1",
        no_token_id="2",
        event_start_ts=1782938400.0,
        event_end_ts=1782938700.0,
        event_slug="btc-updown-5m-1782938400",
    )


def test_classify_expired_market_skipped() -> None:
    result = _sample_result()
    now = result.event_end_ts + 120.0
    assert (
        classify_market_recording_eligibility(
            result,
            now_ts=now,
            post_close_grace_s=60.0,
            skip_expired_markets=True,
        )
        == EXPIRED
    )


def test_classify_future_market_beyond_lead_deferred() -> None:
    result = _sample_result()
    now = result.event_start_ts - 120.0
    assert (
        classify_market_recording_eligibility(
            result,
            now_ts=now,
            pre_open_recording_lead_s=60.0,
        )
        == TOO_EARLY
    )


def test_classify_future_market_within_lead_allowed() -> None:
    result = _sample_result()
    now = result.event_start_ts - 30.0
    assert (
        classify_market_recording_eligibility(
            result,
            now_ts=now,
            pre_open_recording_lead_s=60.0,
        )
        == ELIGIBLE
    )


def test_classify_active_market_allowed() -> None:
    result = _sample_result()
    now = result.event_start_ts + 60.0
    assert (
        classify_market_recording_eligibility(
            result,
            now_ts=now,
            pre_open_recording_lead_s=60.0,
        )
        == ELIGIBLE
    )


def test_classify_market_within_post_close_grace_allowed() -> None:
    result = _sample_result()
    now = result.event_end_ts + 30.0
    assert (
        classify_market_recording_eligibility(
            result,
            now_ts=now,
            post_close_grace_s=60.0,
            skip_expired_markets=True,
        )
        == ELIGIBLE
    )


@pytest.mark.asyncio
async def test_run_market_discovery_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from tyrex_pm.ingestion.market_discovery import run_market_discovery

    seen: list[str] = []

    async def _on_discovered(result: MarketDiscoveryResult) -> None:
        seen.append(result.market_id)

    monkeypatch.setattr(
        "tyrex_pm.ingestion.market_discovery.btc_5m_window_start_timestamps",
        lambda **kwargs: [1782938400],
    )
    monkeypatch.setattr(
        "tyrex_pm.ingestion.market_discovery.fetch_btc_5m_event_by_slug",
        lambda slug, **kwargs: SAMPLE_EVENT,
    )
    monkeypatch.setattr(
        "tyrex_pm.ingestion.market_discovery.classify_market_recording_eligibility",
        lambda *args, **kwargs: ELIGIBLE,
    )

    stop = asyncio.Event()

    async def _stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    asyncio.create_task(_stop_soon())
    await run_market_discovery(stop=stop, on_market_discovered=_on_discovered, poll_interval_s=0.01)
    assert seen.count("btc_5m_20260701_2045") == 1


@pytest.mark.asyncio
async def test_run_market_discovery_skips_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from tyrex_pm.ingestion.market_discovery import run_market_discovery

    seen: list[str] = []
    skipped: list[str] = []

    async def _on_discovered(result: MarketDiscoveryResult) -> None:
        seen.append(result.market_id)

    async def _on_skipped(result: MarketDiscoveryResult, reason: str) -> None:
        skipped.append(f"{result.market_id}:{reason}")

    monkeypatch.setattr(
        "tyrex_pm.ingestion.market_discovery.btc_5m_window_start_timestamps",
        lambda **kwargs: [1782938400],
    )
    monkeypatch.setattr(
        "tyrex_pm.ingestion.market_discovery.fetch_btc_5m_event_by_slug",
        lambda slug, **kwargs: SAMPLE_EVENT,
    )

    stop = asyncio.Event()

    async def _stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    asyncio.create_task(_stop_soon())
    await run_market_discovery(
        stop=stop,
        on_market_discovered=_on_discovered,
        on_market_skipped=_on_skipped,
        poll_interval_s=0.01,
        post_close_grace_s=60.0,
        pre_open_recording_lead_s=60.0,
        skip_expired_markets=True,
    )
    assert seen == []
    assert skipped == ["btc_5m_20260701_2045:expired"]


@pytest.mark.asyncio
async def test_run_market_discovery_defers_far_future(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from tyrex_pm.ingestion.market_discovery import run_market_discovery

    seen: list[str] = []
    result = _sample_result()
    future_start = int(time.time()) + 3600
    future_slug = f"btc-updown-5m-{future_start}"
    future_event = {
        **SAMPLE_EVENT,
        "slug": future_slug,
        "markets": [
            {
                **SAMPLE_EVENT["markets"][0],
                "slug": future_slug,
                "eventStartTime": "2099-01-01T00:00:00Z",
                "endDate": "2099-01-01T00:05:00Z",
            }
        ],
    }

    async def _on_discovered(discovered: MarketDiscoveryResult) -> None:
        seen.append(discovered.market_id)

    monkeypatch.setattr(
        "tyrex_pm.ingestion.market_discovery.btc_5m_window_start_timestamps",
        lambda **kwargs: [future_start],
    )
    monkeypatch.setattr(
        "tyrex_pm.ingestion.market_discovery.fetch_btc_5m_event_by_slug",
        lambda slug, **kwargs: future_event,
    )

    stop = asyncio.Event()

    async def _stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    asyncio.create_task(_stop_soon())
    await run_market_discovery(
        stop=stop,
        on_market_discovered=_on_discovered,
        poll_interval_s=0.01,
        pre_open_recording_lead_s=60.0,
    )
    assert seen == []
    assert result.market_id not in seen
