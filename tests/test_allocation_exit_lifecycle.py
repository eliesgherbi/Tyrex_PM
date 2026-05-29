"""Tests for P4.1 allocation exit-order lifecycle resolution."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import ClientOrderId, RunId, TokenId, VenueOrderId
from tyrex_pm.core.models import ExitIntent, WalletPosition
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_ALLOCATION_LEDGER
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.allocation_exit_lifecycle import (
    apply_exit_fill_for_reservation,
    process_user_ws_allocation_exit,
    release_exit_reservation,
    resolve_exit_order_matched_qty,
)
from tyrex_pm.runtime.allocation_ids import OWNER_SELL_TEST
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.config import parse_app_config
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.pipeline import process_intent_work_unit, reconcile_coordinator
from tyrex_pm.runtime.intent_work import IntentWorkUnit
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.order_store import LocalOrder, OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.allocation_test.strategy import AllocationTestStrategy
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


class _LiveSellOMS:
    async def submit(self, ap, *, market_info=None) -> str:
        del market_info
        from tyrex_pm.core.models import EnterIntent as EI

        if isinstance(ap.intent, EI):
            return json.dumps(
                {
                    "status": "matched",
                    "takingAmount": str(ap.intent.size),
                    "makingAmount": "5",
                    "orderID": "0xbuy",
                    "success": True,
                }
            )
        return json.dumps(
            {
                "status": "live",
                "orderID": "0xexit-live",
                "success": True,
            }
        )

    async def cancel(self, ac) -> str:
        return "cancel_ack"


def _wire_coord(tmp_path: Path) -> tuple[RuntimeCoordinator, JsonlSink, str, AllocationLedger]:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    run_id = str(uuid4())
    sink = JsonlSink(tmp_path / "facts.jsonl")
    sink.__enter__()
    ledger = AllocationLedger(path=tmp_path / "allocation_ledger.json")
    coord.allocation_ledger = ledger
    coord.allocation_ledger_run_id = run_id
    coord.allocation_ledger_sink = sink
    return coord, sink, run_id, ledger


def _facts(tmp_path: Path) -> list[dict]:
    return [
        json.loads(ln)
        for ln in (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]


@pytest.mark.asyncio
async def test_live_resting_then_ws_cancel_releases_reservation(tmp_path: Path) -> None:
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy={
            "kind": "sell_test",
            "enabled": True,
            "token_id": "tok-life",
            "buy": {"enabled": True, "notional_usd": "5", "limit_price": "0.5"},
            "sell": {"enabled": True, "delay_s": 0},
            "run_once": True,
        },
        runtime=dict(_BASE_RUNTIME),
    )
    strat = SellTestStrategy(app.sell_test)  # type: ignore[arg-type]
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)
    tid = TokenId("tok-life")
    await process_intent_work_unit(
        strat.initial_buy_work_units()[0],
        app=app,
        run_id=RunId(run_id),
        strategy=strat,
        coord=coord,
        sink=sink,
        oms=_LiveSellOMS(),
        apply_local_shadow_fill=False,
    )
    coord.wallet.positions[tid] = WalletPosition(
        token_id=tid, qty=Decimal("10"), avg_price_usd=Decimal("0.5")
    )
    wu = IntentWorkUnit(
        intent=ExitIntent(
            token_id=tid,
            side=Side.SELL,
            size=Decimal("8"),
            limit_price=Decimal("0.49"),
            order_style=OrderStyle.GTC,
        ),
        correlation_id="sell-test:tok-life",
        intent_fact_extensions={"source": "sell_test_strategy"},
    )
    await process_intent_work_unit(
        wu,
        app=app,
        run_id=RunId(run_id),
        strategy=strat,
        coord=coord,
        sink=sink,
        oms=_LiveSellOMS(),
        apply_local_shadow_fill=False,
    )
    assert ledger.get_reserved(OWNER_SELL_TEST, tid) == Decimal("8")
    process_user_ws_allocation_exit(
        coord,
        {"type": "CANCELLATION", "id": "0xexit-live"},
    )
    assert ledger.get_reserved(OWNER_SELL_TEST, tid) == Decimal("0")
    assert ledger.get_allocated(OWNER_SELL_TEST, tid) == Decimal("10")
    events = [r["payload"]["event"] for r in _facts(tmp_path) if r["fact_type"] == FACT_TYPE_ALLOCATION_LEDGER]
    assert "allocation_released" in events
    sink.__exit__(None, None, None)


def test_partial_fill_keeps_remaining_reservation(tmp_path: Path) -> None:
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    tid = "tok-partial"
    ledger.apply_buy(OWNER_SELL_TEST, tid, Decimal("10"), correlation_id="buy")
    mut = ledger.reserve_exit(OWNER_SELL_TEST, tid, Decimal("8"), "res-partial")
    ledger.set_reservation_venue_order_id("res-partial", "0xpartial")
    assert apply_exit_fill_for_reservation(
        coord,
        reservation_id="res-partial",
        fill_qty=Decimal("3"),
        source="user_ws",
        dedup_key="fill-1",
        venue_order_id="0xpartial",
    )
    assert ledger.get_allocated(OWNER_SELL_TEST, tid) == Decimal("7")
    assert ledger.get_reserved(OWNER_SELL_TEST, tid) == Decimal("5")
    assert apply_exit_fill_for_reservation(
        coord,
        reservation_id="res-partial",
        fill_qty=Decimal("3"),
        source="user_ws",
        dedup_key="fill-1",
        venue_order_id="0xpartial",
    ) is False
    facts = _facts(tmp_path)
    partial = [r for r in facts if r["payload"].get("event") == "allocation_partial_fill_applied"]
    assert len(partial) == 1
    assert partial[0]["payload"]["filled_qty"] == "3"
    sink.__exit__(None, None, None)


def test_user_ws_update_matched_qty_promotes_fill(tmp_path: Path) -> None:
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    tid = TokenId("tok-ws")
    ledger.apply_buy(OWNER_SELL_TEST, tid, Decimal("8"), correlation_id="buy")
    ledger.reserve_exit(OWNER_SELL_TEST, tid, Decimal("8"), "res-ws")
    ledger.set_reservation_venue_order_id("res-ws", "0xws-order")
    coord.orders.orders[ClientOrderId("res-ws")] = LocalOrder(
        client_order_id=ClientOrderId("res-ws"),
        venue_order_id=VenueOrderId("0xws-order"),
        token_id=tid,
        side=Side.SELL,
        remaining=Decimal("8"),
        original_size=Decimal("8"),
        size_matched=Decimal("0"),
    )
    process_user_ws_allocation_exit(
        coord,
        {
            "type": "UPDATE",
            "id": "0xws-order",
            "asset_id": str(tid),
            "side": "SELL",
            "original_size": "8",
            "size_matched": "8",
        },
    )
    assert ledger.get_allocated(OWNER_SELL_TEST, tid) == Decimal("0")
    assert ledger.get_reserved(OWNER_SELL_TEST, tid) == Decimal("0")
    events = [r["payload"]["event"] for r in _facts(tmp_path) if r["fact_type"] == FACT_TYPE_ALLOCATION_LEDGER]
    assert "allocation_sell_applied" in events
    sink.__exit__(None, None, None)


def test_user_ws_trade_confirmed_no_double_apply(tmp_path: Path) -> None:
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    tid = TokenId("tok-trade")
    ledger.apply_buy(OWNER_SELL_TEST, tid, Decimal("8"), correlation_id="buy")
    ledger.reserve_exit(OWNER_SELL_TEST, tid, Decimal("8"), "res-trade")
    ledger.set_reservation_venue_order_id("res-trade", "0xtrade-order")
    msg = {
        "type": "TRADE",
        "asset_id": str(tid),
        "side": "SELL",
        "size": "8",
        "status": "CONFIRMED",
        "id": "trade-abc",
        "maker_order_id": "0xtrade-order",
    }
    process_user_ws_allocation_exit(coord, msg)
    process_user_ws_allocation_exit(coord, msg)
    assert ledger.get_allocated(OWNER_SELL_TEST, tid) == Decimal("0")
    sell_applied = [
        r for r in _facts(tmp_path) if r["payload"].get("event") == "allocation_sell_applied"
    ]
    assert len(sell_applied) == 1
    sink.__exit__(None, None, None)


def test_allocation_test_live_resting_ledger_state(tmp_path: Path) -> None:
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    strat = AllocationTestStrategy(
        parse_app_config(
            risk=dict(_BASE_RISK),
            strategy={
                "kind": "allocation_test",
                "enabled": True,
                "token_id": "tok-at",
                "buy": {"enabled": True, "notional_usd": "5", "limit_price": "0.5"},
                "owner_b_unauthorized_sell": {"enabled": True},
                "owner_a_sell": {"enabled": True, "delay_s": 0},
                "run_once": True,
            },
            runtime=dict(_BASE_RUNTIME),
        ).allocation_test  # type: ignore[arg-type]
    )
    ledger.apply_buy("allocation_test_A", "tok-at", Decimal("8"), correlation_id="buy")
    ledger.reserve_exit("allocation_test_A", "tok-at", Decimal("8"), "res-a")
    strat._sell_outcome = "live_resting"  # noqa: SLF001
    assert strat.verify_final_ledger(coord) is True
    assert ledger.get_allocated("allocation_test_A", "tok-at") == Decimal("8")
    assert ledger.get_reserved("allocation_test_A", "tok-at") == Decimal("8")
    sink.__exit__(None, None, None)
