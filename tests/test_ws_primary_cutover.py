"""WS-primary cutover behavior tests (M8 D2)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.ids import TokenId
from tyrex_pm.ingestion.market_stream import apply_market_message
from tyrex_pm.market_data.decision_gate import rest_poll_should_run, ws_primary_enabled
from tyrex_pm.market_data.models import BookSource
from tyrex_pm.runtime.config import parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.market_update_coordinator import attach_coordinator_to_authoritative_store
from tyrex_pm.state.market_store import BookLevel, MarketStateStore
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ws"
YES = TokenId("93600127953453226164027182960250028230847035010973306659500433874577010899527")


def _ws_primary_app(**md_over):
    md = {
        "enabled": True,
        "websocket": {"primary_enabled": True, "shadow_enabled": False},
        "rest": {"poll_enabled": False, "bootstrap_on_startup": True},
        "quality": {"enforcement_mode": "enforce"},
    }
    md.update(md_over)
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
                "market_id": "m1",
                "yes_token_id": str(YES),
                "no_token_id": "no-tok",
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
            "market_data": md,
            "execution": {"planner": {"enabled": True}},
        },
    )


def test_ws_primary_config_parsed() -> None:
    app = _ws_primary_app()
    assert ws_primary_enabled(app)
    assert not app.runtime.market_data.websocket.shadow_enabled
    assert not app.runtime.market_data.rest.poll_enabled


def test_rest_poll_disabled_when_ws_primary_connected() -> None:
    app = _ws_primary_app()
    assert not rest_poll_should_run(app, ws_connected=True)
    assert not rest_poll_should_run(app, ws_connected=False)


def test_ws_fixture_writes_authoritative_store_with_ws_primary_source() -> None:
    msg = json.loads((FIXTURES / "market_book.json").read_text(encoding="utf-8"))
    auth = MarketStateStore()
    shadow = MarketStateStore()
    assert apply_market_message(auth, msg, source=BookSource.WEBSOCKET) is True
    cap = auth.capture(YES)
    assert cap is not None
    assert cap.source == BookSource.WEBSOCKET
    assert cap.source_quality == "ws_primary"
    assert shadow.capture(YES) is None


def test_coordinator_wires_authoritative_only() -> None:
    from tyrex_pm.runtime.market_update_coordinator import MarketUpdateCoordinator

    app = _ws_primary_app()
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore())
    coord.market_state = MarketStateStore()
    coord.market_state_shadow = MarketStateStore()
    coordinator = MarketUpdateCoordinator(debounce_ms=50)
    attach_coordinator_to_authoritative_store(coord, coordinator)
    coord.market_state.apply_book(
        YES,
        [BookLevel(Decimal("0.48"), Decimal("100"))],
        [BookLevel(Decimal("0.52"), Decimal("100"))],
        source=BookSource.WEBSOCKET,
    )
    assert coordinator._coalesce_count >= 1
    before = coordinator._coalesce_count
    coord.market_state_shadow.apply_book(
        YES,
        [BookLevel(Decimal("0.40"), Decimal("100"))],
        [BookLevel(Decimal("0.60"), Decimal("100"))],
        source=BookSource.WEBSOCKET,
    )
    assert coordinator._coalesce_count == before


def test_rollback_rest_authoritative_shadow_optional() -> None:
    app = parse_app_config(
        risk={
            "notional": {"min_usd": "0.01", "max_usd": "1000", "max_policy": "cap"},
            "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
            "venue_min_size": {"enabled": False},
            "capital": {"enabled": False},
        },
        strategy={"kind": "paired_binary", "enabled": True, "paired_binary": {"owner_id": "pb", "market_id": "m", "yes_token_id": "1", "no_token_id": "2", "position_size": "5", "max_pair_entry_cost": "1.02", "max_spread_yes": "0.02", "max_spread_no": "0.02", "pair_stop_loss_pct": "0.04", "pair_take_profit_pct": "0.10", "slippage_buffer": "0.005"}},
        runtime={
            "execution_mode": "shadow",
            "market_data": {
                "enabled": True,
                "websocket": {"primary_enabled": False, "shadow_enabled": True},
                "rest": {"poll_enabled": True},
                "quality": {"enforcement_mode": "observe_only"},
            },
            "execution": {"planner": {"enabled": True}},
        },
    )
    assert not ws_primary_enabled(app)
    assert app.runtime.market_data.websocket.shadow_enabled
    assert app.runtime.market_data.rest.poll_enabled
    assert app.runtime.market_data.quality.enforcement_mode == "observe_only"
