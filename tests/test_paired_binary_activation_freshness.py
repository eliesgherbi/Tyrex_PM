"""Activation freshness gating (Group E4)."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import WalletPosition
from tyrex_pm.core.time import utc_now
from tyrex_pm.market_data.decision_freshness import activation_may_proceed
from tyrex_pm.market_data.models import BookLevel, BookSource, SourceQuality
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import ShadowBootstrapConfig, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.paired_binary_run import _try_activate_when_ready
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.entry_eval import read_leg_book
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"


def _ws_primary_app(**md_kw) -> object:
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
                "yes_token_id": YES,
                "no_token_id": NO,
                "position_size": "5",
                "max_pair_entry_cost": "1.02",
                "max_spread_yes": "0.02",
                "max_spread_no": "0.02",
                "pair_stop_loss_pct": "0.04",
                "pair_take_profit_pct": "0.10",
                "slippage_buffer": "0.005",
                "activation_gap_retry_s": 5,
                "use_fixture_book": True,
                "fixture_yes_bid": "0.48",
                "fixture_yes_ask": "0.49",
                "fixture_no_bid": "0.50",
                "fixture_no_ask": "0.51",
            },
        },
        runtime={
            "execution_mode": "shadow",
            "shadow_bootstrap": {"usdc_balance": "1", "usdc_allowance": "1"},
            "market_data": {
                "enabled": True,
                "max_book_age_s": 5,
                "websocket": {"primary_enabled": True, "shadow_enabled": False},
                "quality": {"enforcement_mode": "enforce", "require_ws_primary_for_entry": True},
                **md_kw,
            },
            "execution": {"planner": {"enabled": True}},
            "observability": {"emit_decision_snapshot": True},
        },
    )


def _coord(tmp_path: Path) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    apply_shadow_bootstrap(
        coord.wallet,
        ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")),
    )
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    return coord


def _seed_inventory(coord: RuntimeCoordinator, cfg) -> PairedBinaryRuntimeState:
    now = utc_now()
    store: MarketStateStore = coord.market_state
    store.apply_book(
        TokenId(YES),
        [BookLevel(Decimal("0.48"), Decimal("10000"))],
        [BookLevel(Decimal("0.49"), Decimal("10000"))],
        source=BookSource.WEBSOCKET,
        source_quality=SourceQuality.WS_PRIMARY,
        received_ts=now,
    )
    store.apply_book(
        TokenId(NO),
        [BookLevel(Decimal("0.50"), Decimal("10000"))],
        [BookLevel(Decimal("0.51"), Decimal("10000"))],
        source=BookSource.WEBSOCKET,
        source_quality=SourceQuality.WS_PRIMARY,
        received_ts=now,
    )
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(YES), Decimal("5"), correlation_id="y")
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(NO), Decimal("5"), correlation_id="n")
    coord.wallet.positions[TokenId(YES)] = WalletPosition(
        token_id=TokenId(YES), qty=Decimal("5"), avg_price_usd=Decimal("0.49")
    )
    coord.wallet.positions[TokenId(NO)] = WalletPosition(
        token_id=TokenId(NO), qty=Decimal("5"), avg_price_usd=Decimal("0.51")
    )
    return PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.BOTH_LEGS_FILLED,
        pair_correlation_id="pc-act",
        yes_entry=Decimal("0.49"),
        no_entry=Decimal("0.51"),
        effective_qty=Decimal("5"),
        loss_budget=Decimal("0.04"),
    )


def _apply_stale_ws(coord: RuntimeCoordinator, age_ms: int) -> None:
    ts = utc_now() - timedelta(milliseconds=age_ms)
    store: MarketStateStore = coord.market_state
    for tid in (YES, NO):
        store.apply_book(
            TokenId(tid),
            [BookLevel(Decimal("0.48"), Decimal("100"))],
            [BookLevel(Decimal("0.52"), Decimal("100"))],
            source=BookSource.WEBSOCKET,
            received_ts=ts,
            source_quality=SourceQuality.WS_PRIMARY,
        )


def test_activation_fresh_ws_passes_gate(tmp_path: Path) -> None:
    app = _ws_primary_app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    _seed_inventory(coord, cfg)
    result = activation_may_proceed(app=app, coord=coord, cfg=cfg, size=Decimal("5"))
    assert result.fresh is True
    assert result.report is not None
    assert result.report.verdict.value == "pass"


@pytest.mark.asyncio
async def test_activation_stale_ws_deferred(tmp_path: Path) -> None:
    app = _ws_primary_app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    state = _seed_inventory(coord, cfg)
    _apply_stale_ws(coord, 1760)
    yes_book = read_leg_book(coord.market_state, TokenId(YES), max_book_age_s=cfg.max_book_age_s)
    no_book = read_leg_book(coord.market_state, TokenId(NO), max_book_age_s=cfg.max_book_age_s)
    facts_path = tmp_path / "facts-stale-act.jsonl"
    with JsonlSink(facts_path) as sink:
        await _try_activate_when_ready(
                app=app,
                run_id=RunId("r-stale"),
                coord=coord,
                sink=sink,
                oms=None,
                strategy=None,
                cfg=cfg,
                state=state,
                yes_book=yes_book,
                no_book=no_book,
                apply_local_shadow_fill=True,
                live_clob_client=None,
                recheck=False,
        )
    rows = [json.loads(x) for x in facts_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert state.phase == PairedBinaryPhase.ACTIVATION_PENDING_RECHECK
    assert any(
        r.get("fact_type") == "paired_binary_activation_gap_recheck"
        and (r.get("payload") or {}).get("reason") == "activation_book_age_stale"
        for r in rows
    )
    assert not any(r.get("fact_type") == "decision_snapshot" and (r.get("payload") or {}).get("decision_type") == "activation" for r in rows)


@pytest.mark.asyncio
async def test_activation_fresh_emits_pass_snapshot(tmp_path: Path) -> None:
    app = _ws_primary_app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    state = _seed_inventory(coord, cfg)
    yes_book = read_leg_book(coord.market_state, TokenId(YES), max_book_age_s=cfg.max_book_age_s)
    no_book = read_leg_book(coord.market_state, TokenId(NO), max_book_age_s=cfg.max_book_age_s)
    facts_path = tmp_path / "facts-fresh-act.jsonl"
    with JsonlSink(facts_path) as sink:
        await _try_activate_when_ready(
            app=app,
            run_id=RunId("r-fresh"),
            coord=coord,
            sink=sink,
            oms=None,
            strategy=None,
            cfg=cfg,
            state=state,
            yes_book=yes_book,
            no_book=no_book,
            apply_local_shadow_fill=True,
            live_clob_client=None,
            recheck=False,
        )
    rows = [json.loads(x) for x in facts_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    act = [
        r
        for r in rows
        if r.get("fact_type") == "decision_snapshot" and (r.get("payload") or {}).get("decision_type") == "activation"
    ]
    assert len(act) == 1
    assert act[0]["payload"]["quality_report"]["verdict"] == "pass"
