"""Market readiness state machine tests."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.market_data.models import BookLevel, BookSource
from tyrex_pm.market_data.quality import CRYPTO_5M_PROFILE, DataQualityGate, DataQualityGateConfig
from tyrex_pm.market_data.readiness import MarketReadinessState, MarketReadinessTracker
from tyrex_pm.state.market_store import MarketStateStore

YES = TokenId("yes-r")
NO = TokenId("no-r")


def _store(*, age_ms: int = 100) -> MarketStateStore:
    store = MarketStateStore()
    ts = utc_now() - timedelta(milliseconds=age_ms)
    for tok in (YES, NO):
        store.apply_book(
            tok,
            [BookLevel(Decimal("0.48"), Decimal("100"))],
            [BookLevel(Decimal("0.52"), Decimal("100"))],
            source=BookSource.WEBSOCKET,
            received_ts=ts,
        )
    return store


def test_rest_bootstrap_never_trading_enabled() -> None:
    tracker = MarketReadinessTracker(yes_token_id=YES, no_token_id=NO)
    tracker.note_rest_bootstrapped()
    assert tracker.state == MarketReadinessState.REST_BOOTSTRAPPED
    assert not tracker.allows_trading()


def test_trading_enabled_after_ws_and_quality_pass() -> None:
    gate = DataQualityGate(
        DataQualityGateConfig(profiles={"crypto_5m": CRYPTO_5M_PROFILE})
    )
    tracker = MarketReadinessTracker(yes_token_id=YES, no_token_id=NO)
    tracker.note_rest_bootstrapped()
    tracker.note_ws_connected()
    state = tracker.refresh(_store(), gate=gate, pair_id="p1", size=Decimal("5"))
    assert state == MarketReadinessState.TRADING_ENABLED
    assert tracker.allows_trading()


def test_ws_disconnect_pauses() -> None:
    tracker = MarketReadinessTracker(yes_token_id=YES, no_token_id=NO)
    tracker.note_ws_connected()
    tracker.note_ws_disconnected()
    assert tracker.state == MarketReadinessState.PAUSED


def test_readiness_transitions_recorded() -> None:
    tracker = MarketReadinessTracker(yes_token_id=YES, no_token_id=NO)
    tracker.note_rest_bootstrapped()
    tracker.note_ws_connected()
    assert any(t["to"] == MarketReadinessState.WS_CONNECTED.value for t in tracker.transitions)


def test_paused_resumes_to_trading_enabled_after_fresh_ws_books() -> None:
    gate = DataQualityGate(
        DataQualityGateConfig(profiles={"crypto_5m": CRYPTO_5M_PROFILE})
    )
    tracker = MarketReadinessTracker(yes_token_id=YES, no_token_id=NO)
    tracker.note_rest_bootstrapped()
    tracker.note_ws_connected()
    store = _store()
    tracker.refresh(store, gate=gate, pair_id="p1", size=Decimal("5"))
    assert tracker.allows_trading()
    tracker.note_reconnect_gap()
    assert tracker.state == MarketReadinessState.PAUSED
    tracker.note_rest_recovery()
    tracker.note_ws_connected()
    tracker.refresh(store, gate=gate, pair_id="p1", size=Decimal("5"))
    assert tracker.allows_trading()
