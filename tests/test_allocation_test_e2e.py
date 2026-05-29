"""Shadow golden-chain tests for allocation_test strategy."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.ids import RunId
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_ALLOCATION_LEDGER,
    FACT_TYPE_HEALTH,
    FACT_TYPE_OMS_SUBMIT,
    FACT_TYPE_RISK,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.allocation_ids import DEFAULT_ALLOCATION_TEST_OWNER_A
from tyrex_pm.runtime.config import parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.pipeline import process_intent_work_unit
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.allocation_test.strategy import (
    PHASE_DONE,
    AllocationTestStrategy,
)


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


def _allocation_test_app(token_id: str = "tok-golden") -> object:
    return parse_app_config(
        risk=dict(_BASE_RISK),
        strategy={
            "kind": "allocation_test",
            "enabled": True,
            "token_id": token_id,
            "owner_a_id": "allocation_test_A",
            "owner_b_id": "allocation_test_B",
            "buy": {"enabled": True, "notional_usd": "5", "limit_price": "0.50"},
            "owner_b_unauthorized_sell": {"enabled": True, "size_mode": "match_owner_a_buy"},
            "owner_a_sell": {
                "enabled": True,
                "delay_s": 0,
                "pricing_mode": "auto",
                "aggression_ticks": 0,
                "limit_price": "0.01",
            },
            "run_once": True,
        },
        runtime=dict(_BASE_RUNTIME),
    )


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


async def _run_shadow_chain(tmp_path: Path) -> tuple[AllocationTestStrategy, AllocationLedger, list[dict]]:
    app = _allocation_test_app()
    assert app.allocation_test is not None
    strat = AllocationTestStrategy(app.allocation_test)
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)
    rid = RunId(run_id)
    oms = ShadowOMS()

    for wu in strat.owner_a_buy_work_units():
        await process_intent_work_unit(
            wu,
            app=app,
            run_id=rid,
            strategy=strat,
            coord=coord,
            sink=sink,
            oms=oms,
            apply_local_shadow_fill=True,
        )

    assert strat.buy_submit_succeeded
    assert strat.check_owner_b_prerequisites_visible(coord)
    strat.mark_owner_a_allocation_visible()

    strat.attempt_owner_b_unauthorized_sell(coord, sink, run_id)

    wu = strat.build_owner_a_sell_work_unit(coord)
    assert wu is not None
    await process_intent_work_unit(
        wu,
        app=app,
        run_id=rid,
        strategy=strat,
        coord=coord,
        sink=sink,
        oms=oms,
        apply_local_shadow_fill=True,
    )

    strat.verify_final_ledger(coord)
    facts = _facts(tmp_path)
    sink.__exit__(None, None, None)
    return strat, ledger, facts


async def _run_shadow_chain_live_resting_sell(
    tmp_path: Path,
) -> tuple[AllocationTestStrategy, AllocationLedger, list[dict]]:
    """Live-style path: SELL ack status=live keeps reservation, allocation unchanged."""
    from tyrex_pm.core.ids import TokenId
    from tyrex_pm.core.models import WalletPosition
    from tyrex_pm.strategies.allocation_test.strategy import PHASE_OWNER_A_ALLOCATION_VISIBLE

    app = _allocation_test_app()
    assert app.allocation_test is not None
    strat = AllocationTestStrategy(app.allocation_test)
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)
    rid = RunId(run_id)
    oms = _StatusOMS(sell_status="live")

    for wu in strat.owner_a_buy_work_units():
        await process_intent_work_unit(
            wu,
            app=app,
            run_id=rid,
            strategy=strat,
            coord=coord,
            sink=sink,
            oms=oms,
            apply_local_shadow_fill=False,
        )

    tid = TokenId(app.allocation_test.token_id)
    coord.wallet.positions[tid] = WalletPosition(
        token_id=tid, qty=Decimal("10"), avg_price_usd=Decimal("0.5")
    )
    strat._phase = PHASE_OWNER_A_ALLOCATION_VISIBLE  # noqa: SLF001
    strat._owner_a_buy_size = Decimal("10")  # noqa: SLF001
    strat.attempt_owner_b_unauthorized_sell(coord, sink, run_id)

    wu = strat.build_owner_a_sell_work_unit(coord)
    assert wu is not None
    await process_intent_work_unit(
        wu,
        app=app,
        run_id=rid,
        strategy=strat,
        coord=coord,
        sink=sink,
        oms=oms,
        apply_local_shadow_fill=False,
    )
    strat.verify_final_ledger(coord)
    facts = _facts(tmp_path)
    sink.__exit__(None, None, None)
    return strat, ledger, facts


class _StatusOMS:
    def __init__(self, *, sell_status: str) -> None:
        self._sell_status = sell_status

    async def submit(self, ap, *, market_info=None) -> str:
        del market_info
        from tyrex_pm.core.enums import Side
        from tyrex_pm.core.models import EnterIntent

        if isinstance(ap.intent, EnterIntent) and ap.intent.side == Side.BUY:
            return json.dumps(
                {
                    "status": "matched",
                    "takingAmount": str(ap.intent.size),
                    "makingAmount": "4",
                    "orderID": "0xbuy",
                    "success": True,
                }
            )
        return json.dumps(
            {
                "status": self._sell_status,
                "orderID": "0xsell",
                "success": True,
            }
        )

    async def cancel(self, ac) -> str:
        return "cancel_ack"


@pytest.mark.asyncio
async def test_shadow_full_chain_golden(tmp_path: Path) -> None:
    strat, ledger, facts = await _run_shadow_chain(tmp_path)
    assert strat.phase == PHASE_DONE
    assert strat.is_done()

    alloc_events = [
        r["payload"]["event"]
        for r in facts
        if r["fact_type"] == FACT_TYPE_ALLOCATION_LEDGER
    ]
    assert "allocation_buy_applied" in alloc_events
    assert "allocation_sell_applied" in alloc_events

    blocked = [
        r
        for r in facts
        if r["fact_type"] == FACT_TYPE_HEALTH
        and r["payload"].get("event") == "allocation_test_unauthorized_sell_blocked"
    ]
    assert len(blocked) == 1
    assert Decimal(blocked[0]["payload"]["wallet_position_qty"]) > 0
    assert Decimal(blocked[0]["payload"]["allocated_available"]) == 0

    owner_b_oms = [
        r
        for r in facts
        if r["fact_type"] == FACT_TYPE_OMS_SUBMIT
        and str(r.get("correlation_id", "")).endswith(":B")
    ]
    assert owner_b_oms == []

    tid = strat.cfg.token_id
    assert ledger.get_allocated(DEFAULT_ALLOCATION_TEST_OWNER_A, tid) == Decimal("0")
    assert strat.sell_outcome == "matched"


@pytest.mark.asyncio
async def test_live_style_resting_sell_keeps_allocation_reserved(tmp_path: Path) -> None:
    strat, ledger, facts = await _run_shadow_chain_live_resting_sell(tmp_path)
    assert strat.sell_outcome == "live_resting"
    assert strat.phase == PHASE_DONE
    tid = strat.cfg.token_id
    assert ledger.get_allocated(DEFAULT_ALLOCATION_TEST_OWNER_A, tid) == Decimal("10")
    assert ledger.get_reserved(DEFAULT_ALLOCATION_TEST_OWNER_A, tid) == Decimal("10")
    assert ledger.get_available_allocated(DEFAULT_ALLOCATION_TEST_OWNER_A, tid) == Decimal("0")
    events = [r["payload"]["event"] for r in facts if r["fact_type"] == FACT_TYPE_ALLOCATION_LEDGER]
    assert "allocation_exit_order_live" in events
    assert "allocation_sell_applied" not in events


@pytest.mark.asyncio
async def test_no_owner_b_sell_oms_submit(tmp_path: Path) -> None:
    _, _, facts = await _run_shadow_chain(tmp_path)
    owner_b_correlation = "allocation_test:tok-golden:B"
    owner_b_oms = [
        r
        for r in facts
        if r["fact_type"] == FACT_TYPE_OMS_SUBMIT
        and r.get("correlation_id") == owner_b_correlation
    ]
    assert owner_b_oms == []


@pytest.mark.asyncio
async def test_buy_denied_produces_no_allocation_buy_applied(tmp_path: Path) -> None:
    app = parse_app_config(
        risk={
            **_BASE_RISK,
            "notional": {"min_usd": "1000", "max_usd": "100000", "max_policy": "deny"},
        },
        strategy={
            "kind": "allocation_test",
            "enabled": True,
            "token_id": "tok-deny",
            "buy": {"enabled": True, "notional_usd": "5", "limit_price": "0.50"},
            "owner_b_unauthorized_sell": {"enabled": True},
            "owner_a_sell": {"enabled": True, "delay_s": 0},
            "run_once": True,
        },
        runtime=dict(_BASE_RUNTIME),
    )
    strat = AllocationTestStrategy(app.allocation_test)  # type: ignore[arg-type]
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)

    for wu in strat.owner_a_buy_work_units():
        await process_intent_work_unit(
            wu,
            app=app,
            run_id=RunId(run_id),
            strategy=strat,
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            apply_local_shadow_fill=True,
        )

    facts = _facts(tmp_path)
    assert strat.is_done()
    assert not any(
        r["fact_type"] == FACT_TYPE_ALLOCATION_LEDGER
        and r["payload"].get("event") == "allocation_buy_applied"
        for r in facts
    )
    assert any(r["fact_type"] == FACT_TYPE_RISK and not r["payload"]["approved"] for r in facts)
    assert ledger.get_allocated("allocation_test_A", "tok-deny") == Decimal("0")
    sink.__exit__(None, None, None)
