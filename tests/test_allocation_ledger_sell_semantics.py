"""Tests for P4 SELL allocation semantics: reserve on submit, sell only on match."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import EnterIntent, ExitIntent, WalletPosition
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_ALLOCATION_LEDGER
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.allocation_ids import OWNER_SELL_TEST
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.config import parse_app_config
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.pipeline import process_intent_work_unit
from tyrex_pm.runtime.intent_work import IntentWorkUnit
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


class _StatusOMS:
    def __init__(self, *, sell_status: str, sell_making_amount: str = "") -> None:
        self._sell_status = sell_status
        self._sell_making_amount = sell_making_amount

    async def submit(self, ap, *, market_info=None) -> str:
        del market_info
        from tyrex_pm.core.models import EnterIntent as EI

        if isinstance(ap.intent, EI) and ap.intent.side == Side.BUY:
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
                "status": self._sell_status,
                "takingAmount": "",
                "makingAmount": self._sell_making_amount,
                "orderID": "0xsell",
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


async def _buy_then_sell(
    tmp_path: Path,
    *,
    oms: _StatusOMS,
    apply_local_shadow_fill: bool,
) -> tuple[AllocationLedger, list[dict], str]:
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy={
            "kind": "sell_test",
            "enabled": True,
            "token_id": "tok-sem",
            "buy": {"enabled": True, "notional_usd": "5", "limit_price": "0.5"},
            "sell": {"enabled": True, "delay_s": 0},
            "run_once": True,
        },
        runtime=dict(_BASE_RUNTIME),
    )
    strat = SellTestStrategy(app.sell_test)  # type: ignore[arg-type]
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)
    tid = TokenId("tok-sem")

    await process_intent_work_unit(
        strat.initial_buy_work_units()[0],
        app=app,
        run_id=RunId(run_id),
        strategy=strat,
        coord=coord,
        sink=sink,
        oms=oms,
        apply_local_shadow_fill=apply_local_shadow_fill,
    )
    coord.wallet.positions[tid] = WalletPosition(
        token_id=tid, qty=Decimal("10"), avg_price_usd=Decimal("0.5")
    )
    sell_wu = IntentWorkUnit(
        intent=ExitIntent(
            token_id=tid,
            side=Side.SELL,
            size=Decimal("8"),
            limit_price=Decimal("0.49"),
            order_style=OrderStyle.GTC,
        ),
        correlation_id="sell-test:tok-sem",
        intent_fact_extensions={"source": "sell_test_strategy"},
    )
    await process_intent_work_unit(
        sell_wu,
        app=app,
        run_id=RunId(run_id),
        strategy=strat,
        coord=coord,
        sink=sink,
        oms=oms,
        apply_local_shadow_fill=apply_local_shadow_fill,
    )
    facts = _facts(tmp_path)
    sink.__exit__(None, None, None)
    return ledger, facts, run_id


@pytest.mark.asyncio
async def test_sell_matched_decrements_allocation(tmp_path: Path) -> None:
    ledger, facts, _ = await _buy_then_sell(
        tmp_path,
        oms=_StatusOMS(sell_status="matched", sell_making_amount="8"),
        apply_local_shadow_fill=False,
    )
    events = [r["payload"]["event"] for r in facts if r["fact_type"] == FACT_TYPE_ALLOCATION_LEDGER]
    assert "allocation_sell_applied" in events
    assert ledger.get_allocated(OWNER_SELL_TEST, "tok-sem") == Decimal("2")
    assert ledger.get_reserved(OWNER_SELL_TEST, "tok-sem") == Decimal("0")


@pytest.mark.asyncio
async def test_sell_live_keeps_reservation_and_allocation(tmp_path: Path) -> None:
    ledger, facts, _ = await _buy_then_sell(
        tmp_path,
        oms=_StatusOMS(sell_status="live"),
        apply_local_shadow_fill=False,
    )
    events = [r["payload"]["event"] for r in facts if r["fact_type"] == FACT_TYPE_ALLOCATION_LEDGER]
    assert "allocation_reserved" in events
    assert "allocation_exit_order_live" in events
    assert "allocation_sell_applied" not in events
    assert ledger.get_allocated(OWNER_SELL_TEST, "tok-sem") == Decimal("10")
    assert ledger.get_reserved(OWNER_SELL_TEST, "tok-sem") == Decimal("8")
    assert ledger.get_available_allocated(OWNER_SELL_TEST, "tok-sem") == Decimal("2")
    live_fact = next(r for r in facts if r["payload"].get("event") == "allocation_exit_order_live")
    assert live_fact["payload"]["available_allocated"] == "2"
    assert live_fact["payload"]["reserved_exit_qty"] == "8"


@pytest.mark.asyncio
async def test_sell_oms_reject_releases_reservation(tmp_path: Path) -> None:
    class _RejectSellOMS(_StatusOMS):
        async def submit(self, ap, *, market_info=None) -> str:
            from tyrex_pm.core.models import EnterIntent as EI

            if isinstance(ap.intent, EI):
                return await super().submit(ap, market_info=market_info)
            from py_clob_client_v2.exceptions import PolyApiException

            raise PolyApiException(error_msg="simulated reject")

    ledger, facts, _ = await _buy_then_sell(
        tmp_path,
        oms=_RejectSellOMS(sell_status="live"),
        apply_local_shadow_fill=False,
    )
    events = [r["payload"]["event"] for r in facts if r["fact_type"] == FACT_TYPE_ALLOCATION_LEDGER]
    assert "allocation_released" in events
    assert ledger.get_allocated(OWNER_SELL_TEST, "tok-sem") == Decimal("10")
    assert ledger.get_available_allocated(OWNER_SELL_TEST, "tok-sem") == Decimal("10")


@pytest.mark.asyncio
async def test_shadow_instant_fill_still_applies_sell(tmp_path: Path) -> None:
    ledger, facts, _ = await _buy_then_sell(
        tmp_path,
        oms=_StatusOMS(sell_status="live"),
        apply_local_shadow_fill=True,
    )
    events = [r["payload"]["event"] for r in facts if r["fact_type"] == FACT_TYPE_ALLOCATION_LEDGER]
    assert "allocation_sell_applied" in events
    assert ledger.get_allocated(OWNER_SELL_TEST, "tok-sem") == Decimal("2")
