"""M8 rollback drill — config-only restore of REST authoritative + shadow WS (Group E Step 5)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.ids import TokenId
from tyrex_pm.ingestion.market_stream import apply_market_message
from tyrex_pm.market_data.decision_gate import should_block_paired_binary_decision, ws_primary_enabled
from tyrex_pm.market_data.models import BookSource
from tyrex_pm.market_data.quality import DecisionContext, EnforcementMode
from tyrex_pm.runtime.config import parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.state.market_store import BookLevel, MarketStateStore
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ws"
YES = TokenId("93600127953453226164027182960250028230847035010973306659500433874577010899527")
NO = "98261242917329382720414284166477944193653462411741333161666117894652193129742"


def _rollback_app():
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
                "no_token_id": NO,
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
                "websocket": {"primary_enabled": False, "shadow_enabled": True},
                "rest": {"poll_enabled": True},
                "quality": {"enforcement_mode": "observe_only"},
            },
            "execution": {"planner": {"enabled": True}},
        },
    )


def test_rollback_rest_authoritative_ws_shadow_only() -> None:
    app = _rollback_app()
    assert not ws_primary_enabled(app)
    assert app.runtime.market_data.websocket.shadow_enabled
    assert app.runtime.market_data.rest.poll_enabled
    assert app.runtime.market_data.quality.enforcement_mode == EnforcementMode.OBSERVE_ONLY.value

    auth = MarketStateStore()
    shadow = MarketStateStore()
    auth.apply_book(
        YES,
        [BookLevel(Decimal("0.99"), Decimal("1"))],
        [BookLevel(Decimal("0.01"), Decimal("1"))],
        source=BookSource.REST_POLL,
    )
    msg = json.loads((FIXTURES / "market_book.json").read_text(encoding="utf-8"))
    apply_market_message(shadow, msg, source=BookSource.WEBSOCKET)
    assert auth.best_bid(YES) == Decimal("0.99")
    assert shadow.best_bid(YES) == Decimal("0.54")
    cap_auth = auth.capture(YES)
    cap_shadow = shadow.capture(YES)
    assert cap_auth is not None and cap_auth.source == BookSource.REST_POLL
    assert cap_shadow is not None and cap_shadow.source == BookSource.WEBSOCKET


def test_rollback_observe_only_does_not_block_entry_on_rest() -> None:
    app = _rollback_app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore())
    store = MarketStateStore()
    for tid in (YES, TokenId(NO)):
        store.apply_book(
            tid,
            [BookLevel(Decimal("0.48"), Decimal("100"))],
            [BookLevel(Decimal("0.52"), Decimal("100"))],
            source=BookSource.REST_POLL,
        )
    coord.market_state = store
    result = should_block_paired_binary_decision(
        app=app,
        coord=coord,
        cfg=cfg,
        context=DecisionContext.ENTRY,
        size=Decimal("5"),
    )
    assert result.allowed
