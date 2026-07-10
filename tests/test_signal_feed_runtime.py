"""Tests for signal_feed_runtime (A0.2)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tyrex_pm.ingestion.price_to_beat_tracker import PriceToBeatTracker
from tyrex_pm.runtime.config import parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.signal_feed_facts import build_signal_feed_health_from_snapshot
from tyrex_pm.runtime.signal_feed_runtime import (
    apply_market_event_to_signal_store,
    signal_feeds_requested,
    start_signal_feeds,
    stop_signal_feeds,
)
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.signal_state_store import SignalStateStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.venue.binance_data.normalize import build_external_btc_tick_event, normalize_book_ticker
from tyrex_pm.venue.polymarket_rtds.normalize import (
    build_price_to_beat_observed_event,
    build_reference_price_tick_event,
    normalize_reference_price_tick,
)

CHAINLINK_MSG = {
    "topic": "crypto_prices_chainlink",
    "type": "update",
    "timestamp": 1753314088421,
    "payload": {"symbol": "btc/usd", "timestamp": 1780000000500, "value": 109812.50},
}


def _recv() -> datetime:
    return datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


def _z_gap_app_with_feeds():
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
        strategy={
            "kind": "z_gap",
            "enabled": True,
            "z_gap": {
                "market_id": "btc_5m_20260703_1200",
                "condition_id": "0xabc",
                "yes_token_id": "111",
                "no_token_id": "222",
                "event_start_ts": 1780000000,
                "event_end_ts": 1780000300,
            },
        },
        runtime={
            "execution_mode": "live",
            "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
            "signal_feeds": {"enabled": True},
            "external_btc": {"enabled": True, "symbol": "BTCUSDT", "streams": ["bookTicker"]},
            "reference_prices": {
                "enabled": True,
                "feeds": ["chainlink"],
                "symbols": ["btc/usd"],
                "emit_price_to_beat": True,
            },
        },
    )


def test_signal_feeds_requested_only_when_enabled() -> None:
    app = _z_gap_app_with_feeds()
    assert signal_feeds_requested(app) is True
    app_off = parse_app_config(
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
        strategy={"kind": "z_gap", "enabled": True, "z_gap": {"market_id": "m", "yes_token_id": "1", "no_token_id": "2"}},
        runtime={"execution_mode": "live", "signal_feeds": {"enabled": False}},
    )
    assert signal_feeds_requested(app_off) is False


def test_apply_market_events_update_store() -> None:
    store = SignalStateStore()
    recv = _recv()
    payload = normalize_book_ticker({"b": "100", "a": "102", "s": "BTCUSDT"}, symbol="BTCUSDT")
    btc_event = build_external_btc_tick_event(payload, recv_ts=recv, venue_cursor="1")
    apply_market_event_to_signal_store(store, btc_event)

    ref_payload = normalize_reference_price_tick(CHAINLINK_MSG, feed="chainlink")
    assert ref_payload is not None
    ref_event = build_reference_price_tick_event(ref_payload, recv_ts=recv)
    apply_market_event_to_signal_store(store, ref_event)

    snap = store.snapshot(now=recv)
    assert snap.binance_price == Decimal("101")
    assert snap.chainlink_price == Decimal("109812.5")


def test_price_to_beat_event_updates_store() -> None:
    store = SignalStateStore()
    recv = _recv()
    event = build_price_to_beat_observed_event(
        market_id="btc_5m_test",
        event_start_ts=1780000000.0,
        event_end_ts=1780000300.0,
        price_to_beat="109812.50",
        price_to_beat_ts=recv,
        price_to_beat_source="polymarket_rtds_chainlink",
        price_to_beat_lag_ms=50.0,
        raw_reference_event_id="abc",
        status="observed",
        recv_ts=recv,
    )
    apply_market_event_to_signal_store(store, event)
    snap = store.snapshot(now=recv)
    assert snap.price_to_beat == Decimal("109812.50")
    assert snap.ptb_status == "observed"


@pytest.mark.asyncio
async def test_start_signal_feeds_mocked_updates_store(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _z_gap_app_with_feeds()
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    stop = asyncio.Event()
    health_events: list[dict] = []

    async def _fake_btc(**kwargs):
        on_event = kwargs["on_event_emitted"]
        recv = _recv()
        payload = normalize_book_ticker({"b": "50000", "a": "50002", "s": "BTCUSDT"}, symbol="BTCUSDT")
        await on_event(build_external_btc_tick_event(payload, recv_ts=recv, venue_cursor="1"))
        await stop.wait()

    async def _fake_ref(**kwargs):
        on_event = kwargs["on_event_emitted"]
        tracker: PriceToBeatTracker | None = kwargs.get("price_to_beat_tracker")
        recv = _recv()
        ref_payload = normalize_reference_price_tick(CHAINLINK_MSG, feed="chainlink")
        ref_event = build_reference_price_tick_event(ref_payload, recv_ts=recv)
        await on_event(ref_event)
        if tracker is not None:
            for derived in tracker.on_reference_tick(ref_event):
                await on_event(derived)
        await stop.wait()

    monkeypatch.setattr("tyrex_pm.ingestion.external_btc.run_external_btc_ingest", _fake_btc)
    monkeypatch.setattr("tyrex_pm.ingestion.reference_prices.run_reference_prices_ingest", _fake_ref)

    state = await start_signal_feeds(
        coord=coord,
        app=app,
        stop=stop,
        market_id="btc_5m_20260703_1200",
        event_start_ts=1780000000.0,
        event_end_ts=1780000300.0,
        on_health=lambda payload: health_events.append(payload),
    )
    await asyncio.sleep(0.05)
    assert coord.signal_state is not None
    snap = coord.signal_state.snapshot(now=_recv())
    assert snap.binance_price == Decimal("50001")
    assert snap.chainlink_price == Decimal("109812.5")
    assert snap.price_to_beat == Decimal("109812.5")
    assert any(e.get("kind") == "basis_computed" for e in health_events)
    stop.set()
    await stop_signal_feeds(state)


@pytest.mark.asyncio
async def test_stop_signal_feeds_cancels_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()

    async def _hang(**kwargs):
        started.set()
        await kwargs["stop"].wait()

    monkeypatch.setattr("tyrex_pm.ingestion.external_btc.run_external_btc_ingest", _hang)
    monkeypatch.setattr("tyrex_pm.ingestion.reference_prices.run_reference_prices_ingest", _hang)

    app = _z_gap_app_with_feeds()
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    stop = asyncio.Event()
    state = await start_signal_feeds(coord=coord, app=app, stop=stop)
    await asyncio.wait_for(started.wait(), timeout=2.0)
    await stop_signal_feeds(state)
    assert state.binance_task is None
    assert state.reference_task is None


def test_non_z_gap_coord_has_no_signal_state_by_default() -> None:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    assert coord.signal_state is None


def test_feed_health_payload_contract() -> None:
    store = SignalStateStore()
    recv = _recv()
    store.update_binance(Decimal("100"), source_ts=recv, recv_ts=recv, stream="bookTicker")
    store.update_chainlink(Decimal("99"), source_ts=recv, recv_ts=recv)
    snap = store.snapshot(now=recv)
    payload = build_signal_feed_health_from_snapshot(snap, feed="binance")
    assert payload["feed"] == "binance"
    assert payload["connected"] is True
    assert "age_ms" in payload
    assert "stale" in payload
