"""M8 controlled reconnect drill — readiness + REST recovery path (Group E2 Step 5).

Simulated (not live) drill for acceptance criteria 8/9 when no natural WS disconnect
occurs during a long validation run.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
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

YES = TokenId("yes-reconnect-drill")
NO = TokenId("no-reconnect-drill")


def _app():
    return parse_app_config(
        risk={
            "notional": {"min_usd": "1", "max_usd": "15", "max_policy": "cap"},
            "deployment": {"token_cap_usd": "20", "portfolio_cap_usd": "30"},
            "venue_min_size": {"enabled": False},
            "capital": {"enabled": False},
        },
        strategy={
            "kind": "paired_binary",
            "enabled": True,
            "paired_binary": {
                "owner_id": "paired_binary",
                "market_id": "reconnect-drill",
                "yes_token_id": str(YES),
                "no_token_id": str(NO),
                "position_size": "5",
                "max_pair_entry_cost": "1.015",
                "max_spread_yes": "0.02",
                "max_spread_no": "0.02",
                "pair_stop_loss_pct": "0.09",
                "pair_take_profit_pct": "0.3",
                "slippage_buffer": "0.005",
            },
        },
        runtime={
            "execution_mode": "shadow",
            "market_data": {
                "enabled": True,
                "websocket": {"primary_enabled": True, "shadow_enabled": False},
                "rest": {"poll_enabled": False, "recovery_on_reconnect": True},
                "quality": {"enforcement_mode": "enforce", "require_ws_primary_for_entry": True},
            },
            "execution": {"planner": {"enabled": True}},
        },
    )


def _ws_books(store: MarketStateStore) -> None:
    ts = utc_now() - timedelta(milliseconds=50)
    for tok in (YES, NO):
        store.apply_book(
            tok,
            [BookLevel(Decimal("0.48"), Decimal("500"))],
            [BookLevel(Decimal("0.52"), Decimal("500"))],
            source=BookSource.WEBSOCKET,
            received_ts=ts,
            source_quality=SourceQuality.WS_PRIMARY,
        )
        store.set_reconnect_gap(tok, False)


def test_m8_reconnect_drill_pause_blocks_then_resume_trading_enabled(tmp_path: Path) -> None:
    """Simulated WS gap → PAUSED → REST_RECOVERY → fresh WS both legs → TRADING_ENABLED."""
    app = _app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore())
    store = MarketStateStore()
    _ws_books(store)
    coord.market_state = store
    tracker = MarketReadinessTracker(yes_token_id=YES, no_token_id=NO)
    coord.market_readiness_tracker = tracker
    tracker.note_rest_bootstrapped()
    tracker.note_ws_connected()
    refresh_market_readiness(coord, app, cfg)
    assert tracker.allows_trading()

    tracker.note_reconnect_gap()
    assert tracker.state == MarketReadinessState.PAUSED
    assert not tracker.allows_new_entries()

    tracker.note_rest_recovery()
    assert tracker.state == MarketReadinessState.PAUSED
    assert not tracker.yes_ws_ready
    assert not tracker.no_ws_ready

    for tok in (YES, NO):
        store.apply_book(
            tok,
            [BookLevel(Decimal("0.48"), Decimal("500"))],
            [BookLevel(Decimal("0.52"), Decimal("500"))],
            source=BookSource.REST_RECOVERY,
            received_ts=utc_now(),
        )

    _ws_books(store)
    tracker.note_ws_connected()
    facts_path = tmp_path / "facts.jsonl"
    with JsonlSink(facts_path) as sink:
        refresh_market_readiness(coord, app, cfg, sink=sink, run_id="reconnect-drill")
        emit_readiness_transitions(sink, "reconnect-drill", tracker)

    assert tracker.allows_trading()
    rows = [json.loads(x) for x in facts_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    transitions = [r["payload"] for r in rows if r["fact_type"] == "market_readiness_transition"]
    assert any(t.get("to") == "paused" and t.get("reason") == "reconnect_gap" for t in transitions)
    assert any(t.get("to") == "trading_enabled" for t in transitions)
