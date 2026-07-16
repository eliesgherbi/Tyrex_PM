"""WS-primary readiness runtime wiring tests (M8 D1)."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.execution.planner import build_quality_gate_from_config
from tyrex_pm.market_data.models import BookLevel, BookSource, SourceQuality
from tyrex_pm.market_data.readiness import MarketReadinessState, MarketReadinessTracker
from tyrex_pm.market_data.readiness_runtime import (
    emit_readiness_transitions,
    refresh_market_readiness,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.state.market_store import MarketStateStore
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore

YES = TokenId("yes-wpr")
NO = TokenId("no-wpr")


def _ws_primary_app():
    return parse_app_config(
        risk={
            "notional": {"min_usd": "0.01", "max_usd": "1000", "max_policy": "cap"},
            "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
            "venue_min_size": {"enabled": False},
            "capital": {"enabled": False},
        },
        strategy={
            "kind": "paired_binary",
            "enabled": True,
            "paired_binary": {
                "owner_id": "paired_binary",
                "market_id": "m-ws",
                "yes_token_id": str(YES),
                "no_token_id": str(NO),
                "position_size": "5",
                "max_pair_entry_cost": "1.02",
                "max_spread_yes": "0.02",
                "max_spread_no": "0.02",
                "pair_stop_loss_pct": "0.04",
                "pair_take_profit_pct": "0.10",
                "slippage_buffer": "0.005",
            },
        },
        runtime={
            "execution_mode": "shadow",
            "market_data": {
                "enabled": True,
                "websocket": {"primary_enabled": True},
                "quality": {"enforcement_mode": "enforce"},
            },
            "execution": {"planner": {"enabled": True}},
        },
    )


def _fresh_ws_store() -> MarketStateStore:
    store = MarketStateStore()
    ts = utc_now() - timedelta(milliseconds=100)
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
    app = _ws_primary_app()
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore())
    cfg = app.paired_binary
    assert cfg is not None
    from tyrex_pm.market_data.readiness_runtime import ensure_market_readiness_tracker

    tracker = ensure_market_readiness_tracker(coord, app, cfg)
    assert tracker is not None
    tracker.note_rest_bootstrapped()
    assert tracker.state == MarketReadinessState.REST_BOOTSTRAPPED
    assert not tracker.allows_trading()


def test_refresh_reaches_trading_enabled(tmp_path: Path) -> None:
    app = _ws_primary_app()
    from tyrex_pm.state.order_store import OrderStore
    from tyrex_pm.state.wallet_store import WalletStore

    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore())
    cfg = app.paired_binary
    assert cfg is not None
    coord.market_state = _fresh_ws_store()
    tracker = MarketReadinessTracker(yes_token_id=YES, no_token_id=NO)
    coord.market_readiness_tracker = tracker
    tracker.note_rest_bootstrapped()
    tracker.note_ws_connected()
    facts_path = tmp_path / "facts.jsonl"
    with JsonlSink(facts_path) as sink:
        refresh_market_readiness(coord, app, cfg, sink=sink, run_id="r1")
    assert tracker.allows_trading()
    rows = [json.loads(x) for x in facts_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert any(r["fact_type"] == "market_readiness_transition" for r in rows)


def test_reconnect_gap_blocks_trading() -> None:
    tracker = MarketReadinessTracker(yes_token_id=YES, no_token_id=NO)
    tracker.note_ws_connected()
    tracker.note_reconnect_gap()
    assert not tracker.allows_trading()
    assert tracker.state == MarketReadinessState.PAUSED
