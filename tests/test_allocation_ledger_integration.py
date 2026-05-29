"""Integration tests for P4 allocation ledger runtime wiring."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import ClientOrderId, RunId, TokenId
from tyrex_pm.core.models import ApprovedIntent, EnterIntent, ExitIntent, WalletPosition
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_ALLOCATION_LEDGER,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.config import parse_app_config
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.allocation_ids import OWNER_SELL_TEST
from tyrex_pm.runtime.allocation_runtime import maybe_clamp_allocations_to_venue
from tyrex_pm.runtime.pipeline import process_intent_work_unit, process_scheduled_exit_demo_due
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.sell_test.strategy import SellTestStrategy


_BASE_RISK = {
    "notional": {"min_usd": "0.01", "max_usd": "100", "max_policy": "cap"},
    "deployment": {"token_cap_usd": "500", "portfolio_cap_usd": "5000"},
    "venue_min_size": {"enabled": False},
    "capital": {"enabled": False, "max_wallet_age_s": 120},
    "inventory": {"sell_requires_venue_position": True},
    "concurrency": {"max_orders_in_flight": 8},
    "readiness": {
        "require_wallet_sync": False,
        "max_wallet_age_s_live": 120,
        "require_heartbeat_live": False,
        "require_user_ws_live": False,
    },
}

_BASE_RUNTIME = {
    "execution_mode": "shadow",
    "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
    "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
    "supervisors": {"reconcile_interval_s": 30, "submit_grace_s": 15},
    "logging": {"level": "WARNING"},
    "allocation_ledger": {"enabled": True},
}


def _wire_coord(tmp_path: Path) -> tuple[RuntimeCoordinator, JsonlSink, str, AllocationLedger]:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    run_id = str(uuid4())
    sink = JsonlSink(tmp_path / "facts.jsonl")
    sink.__enter__()
    ledger = AllocationLedger(path=tmp_path / "allocation_ledger.json")
    coord.allocation_ledger = ledger
    coord.allocation_ledger_run_id = run_id
    coord.allocation_ledger_sink = sink
    coord.exit_lifecycle_run_id = run_id
    coord.exit_lifecycle_sink = sink
    return coord, sink, run_id, ledger


def _sell_test_app(token_id: str = "tok-alloc") -> object:
    return parse_app_config(
        risk=dict(_BASE_RISK),
        strategy={
            "kind": "sell_test",
            "enabled": True,
            "token_id": token_id,
            "buy": {"enabled": True, "notional_usd": "5", "limit_price": "0.5"},
            "sell": {"enabled": True, "delay_s": 0},
            "run_once": True,
        },
        runtime=dict(_BASE_RUNTIME),
    )


def _facts(tmp_path: Path) -> list[dict]:
    return [
        json.loads(ln)
        for ln in (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]


@pytest.mark.asyncio
async def test_successful_buy_updates_ledger(tmp_path: Path) -> None:
    app = _sell_test_app()
    assert app.sell_test is not None
    strat = SellTestStrategy(app.sell_test)
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)
    coord.scheduled_exit_demo_try_arm = lambda *, source="post_buy_ack": strat.sell_test_state.try_arm_live_pending(
        coord, source=source
    )
    await process_intent_work_unit(
        strat.initial_buy_work_units()[0],
        app=app,
        run_id=RunId(run_id),
        strategy=strat,
        coord=coord,
        sink=sink,
        oms=ShadowOMS(),
        apply_local_shadow_fill=True,
    )
    tid = app.sell_test.token_id
    assert ledger.get_allocated(OWNER_SELL_TEST, tid) > 0
    events = [r["payload"]["event"] for r in _facts(tmp_path) if r["fact_type"] == FACT_TYPE_ALLOCATION_LEDGER]
    assert "allocation_buy_applied" in events
    sink.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_buy_risk_denied_does_not_update_ledger(tmp_path: Path) -> None:
    app = parse_app_config(
        risk={
            **_BASE_RISK,
            "notional": {"min_usd": "1000", "max_usd": "100000", "max_policy": "deny"},
        },
        strategy={
            "kind": "sell_test",
            "enabled": True,
            "token_id": "tok-deny-alloc",
            "buy": {"enabled": True, "notional_usd": "5", "limit_price": "0.5"},
            "sell": {"enabled": True, "delay_s": 0},
            "run_once": True,
        },
        runtime=dict(_BASE_RUNTIME),
    )
    strat = SellTestStrategy(app.sell_test)
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)
    await process_intent_work_unit(
        strat.initial_buy_work_units()[0],
        app=app,
        run_id=RunId(run_id),
        strategy=strat,
        coord=coord,
        sink=sink,
        oms=ShadowOMS(),
        apply_local_shadow_fill=True,
    )
    assert ledger.get_allocated(OWNER_SELL_TEST, "tok-deny-alloc") == Decimal("0")
    assert not any(r["fact_type"] == FACT_TYPE_ALLOCATION_LEDGER for r in _facts(tmp_path))
    sink.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_successful_sell_reduces_ledger(tmp_path: Path) -> None:
    app = _sell_test_app(token_id="tok-sell-alloc")
    strat = SellTestStrategy(app.sell_test)
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)
    coord.scheduled_exit_demo_try_arm = lambda *, source="post_buy_ack": strat.sell_test_state.try_arm_live_pending(
        coord, source=source
    )
    await process_intent_work_unit(
        strat.initial_buy_work_units()[0],
        app=app,
        run_id=RunId(run_id),
        strategy=strat,
        coord=coord,
        sink=sink,
        oms=ShadowOMS(),
        apply_local_shadow_fill=True,
    )
    before = ledger.get_allocated(OWNER_SELL_TEST, "tok-sell-alloc")
    await asyncio.sleep(0.05)
    await process_scheduled_exit_demo_due(
        strategy=strat,
        app=app,
        run_id=RunId(run_id),
        coord=coord,
        sink=sink,
        oms=ShadowOMS(),
        apply_local_shadow_fill=True,
        live_clob_client=None,
    )
    after = ledger.get_allocated(OWNER_SELL_TEST, "tok-sell-alloc")
    assert after < before
    events = [r["payload"]["event"] for r in _facts(tmp_path) if r["fact_type"] == FACT_TYPE_ALLOCATION_LEDGER]
    assert "allocation_buy_applied" in events
    assert "allocation_sell_applied" in events
    sink.__exit__(None, None, None)


def test_clamp_emits_allocation_fact(tmp_path: Path) -> None:
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    tid = TokenId("tok-clamp")
    ledger.apply_buy(OWNER_SELL_TEST, tid, Decimal("10"))
    coord.wallet.positions[tid] = WalletPosition(
        token_id=tid, qty=Decimal("4"), avg_price_usd=Decimal("0.5")
    )
    maybe_clamp_allocations_to_venue(coord, run_id=run_id)
    assert ledger.get_allocated(OWNER_SELL_TEST, tid) == Decimal("4")
    clamp_rows = [
        r
        for r in _facts(tmp_path)
        if r["fact_type"] == FACT_TYPE_ALLOCATION_LEDGER
        and r["payload"]["event"] == "allocation_clamped"
    ]
    assert len(clamp_rows) == 1
    assert clamp_rows[0]["payload"]["venue_qty"] == "4"
    sink.__exit__(None, None, None)


def test_scheduled_exit_sizes_no_more_than_allocated(tmp_path: Path) -> None:
    app = _sell_test_app(token_id="tok-size-cap")
    strat = SellTestStrategy(app.sell_test)
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)
    tid = TokenId("tok-size-cap")
    ledger.apply_buy(OWNER_SELL_TEST, tid, Decimal("3"))
    coord.wallet.positions[tid] = WalletPosition(
        token_id=tid, qty=Decimal("10"), avg_price_usd=Decimal("0.5")
    )
    ent = EnterIntent(
        token_id=tid,
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    ap = ApprovedIntent(intent=ent, client_order_id=ClientOrderId("c-buy"), run_id=RunId(run_id))
    strat.sell_test_state.register_after_successful_buy(
        ap,
        coord,
        parent_correlation_id="corr-cap",
        execution_mode=app.runtime.execution_mode,
        apply_shadow_fill=True,
    )
    rows = strat.sell_test_state.pop_due_rows(coord)
    assert len(rows) == 1
    assert rows[0].sell_size <= Decimal("3")
    sink.__exit__(None, None, None)
