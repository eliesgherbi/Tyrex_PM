"""Runtime integration tests for validation_harness (P4.5 architecture_enhance)."""

from __future__ import annotations

import argparse
import asyncio
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import URGENCY_URGENT
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_EXECUTION_PLAN,
    FACT_TYPE_INTENT,
    FACT_TYPE_OMS_SUBMIT,
    FACT_TYPE_PROTECTION_REGISTER,
    FACT_TYPE_PROTECTION_TRIGGER,
    FACT_TYPE_RISK,
    FACT_TYPE_SIGNAL_RECEIVED,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import (
    STRATEGY_KIND_VALIDATION_HARNESS,
    VALIDATION_MODE_PROTECTION_REGISTER,
    VALIDATION_MODE_PROTECTION_SL,
    VALIDATION_MODE_PROTECTION_TP,
    VALIDATION_MODE_STALE_BOOK_DENY,
    VALIDATION_MODE_URGENT_EXIT,
    parse_app_config,
    ShadowBootstrapConfig,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.validation_harness_run import run_validation_harness_once
from tyrex_pm.signals.validation_signal import ValidationSignal
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.validation_harness.strategy import ValidationHarnessStrategy

TOKEN = "token-validation-harness"

_RISK = {
    "notional": {"min_usd": "0.01", "max_usd": "100", "max_policy": "cap"},
    "deployment": {"token_cap_usd": "500", "portfolio_cap_usd": "5000"},
    "venue_min_size": {"enabled": False},
    "capital": {"enabled": False, "max_wallet_age_s": 120},
    "concurrency": {"max_orders_in_flight": 8},
    "readiness": {
        "require_wallet_sync": False,
        "max_wallet_age_s_live": 120,
        "require_heartbeat_live": False,
        "require_user_ws_live": False,
    },
    "inventory": {"sell_requires_venue_position": False},
}


def _runtime(*, planner: bool = True, protection: bool = False) -> dict:
    rt = {
        "execution_mode": "shadow",
        "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
        "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
        "supervisors": {"reconcile_interval_s": 30, "submit_grace_s": 15},
        "logging": {"level": "WARNING"},
    }
    if planner:
        rt["market_data"] = {"enabled": True, "max_book_age_s": 5}
        rt["execution"] = {
            "planner": {
                "enabled": True,
                "require_fresh_book_for_urgent": True,
                "max_book_age_s": 5,
            }
        }
    return rt


def _strategy_yaml(**validation_kw) -> dict:
    protection_enabled = bool(validation_kw.pop("protection_enabled", False))
    base = {
        "mode": "normal_entry",
        "owner_id": "validation_harness",
        "token_id": TOKEN,
        "side": "BUY",
        "notional_usd": "5",
        "limit_price": "0.50",
        "order_style": "GTC",
        "run_once": True,
    }
    base.update(validation_kw)
    strat = {
        "kind": "validation_harness",
        "enabled": True,
        "token_id": TOKEN,
        "validation": base,
    }
    if protection_enabled:
        strat["protection"] = {
            "enabled": True,
            "take_profit_pct": "0.10",
            "stop_loss_pct": "0.05",
            "register_on_buy": True,
        }
    return strat


def _wire_reporting_sink(coord: RuntimeCoordinator, sink: JsonlSink, run_id: RunId) -> None:
    coord.allocation_ledger_sink = sink
    coord.allocation_ledger_run_id = run_id


def _coord(tmp_path: Path) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    apply_shadow_bootstrap(
        coord.wallet,
        ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")),
    )
    return coord


def _read(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.mark.asyncio
async def test_validation_harness_normal_entry_uses_process_signals(tmp_path: Path) -> None:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy=_strategy_yaml(),
        runtime=_runtime(),
    )
    assert app.strategy_kind == STRATEGY_KIND_VALIDATION_HARNESS
    facts = tmp_path / "facts.jsonl"
    with patch(
        "tyrex_pm.runtime.validation_harness_run.process_signals", new_callable=AsyncMock
    ) as mock_ps:
        with JsonlSink(facts) as sink:
            n = await run_validation_harness_once(
                app=app,
                run_id=RunId(str(uuid4())),
                coord=_coord(tmp_path),
                sink=sink,
                oms=ShadowOMS(),
                cfg=app.validation_harness,
            )
        assert n == 1
        mock_ps.assert_awaited_once()


@pytest.mark.asyncio
async def test_validation_harness_does_not_poll_guru(tmp_path: Path, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    poll_mock = AsyncMock(side_effect=AssertionError("poll_guru_incremental must not be called"))
    monkeypatch.setattr("tyrex_pm.runtime.app.poll_guru_incremental", poll_mock)

    args = argparse.Namespace(
        repo_root=root,
        strategy=str(root / "config" / "strategies" / "validation_harness.yaml"),
        scenario=str(root / "config" / "scenarios" / "shadow_validation_harness.yaml"),
        state_dir=str(tmp_path / "state"),
        once=True,
        fixture=None,
        max_iterations=None,
        run_name="cli_validation_harness",
    )
    from tyrex_pm.runtime import app as app_mod

    await app_mod.cmd_run(args)
    poll_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_validation_harness_urgent_exit_emits_exit_intent(tmp_path: Path) -> None:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy=_strategy_yaml(
            mode=VALIDATION_MODE_URGENT_EXIT,
            seed_allocation_qty="25",
            limit_price="0.50",
            use_fixture_book=True,
        ),
        runtime=_runtime(),
    )
    facts = tmp_path / "facts.jsonl"
    with JsonlSink(facts) as sink:
        n = await run_validation_harness_once(
            app=app,
            run_id=RunId(str(uuid4())),
            coord=_coord(tmp_path),
            sink=sink,
            oms=ShadowOMS(),
            cfg=app.validation_harness,
        )
    assert n == 1
    rows = _read(facts)
    intents = [r for r in rows if r["fact_type"] == FACT_TYPE_INTENT]
    assert intents
    payload = intents[0]["payload"]
    assert payload.get("side") == "SELL" or payload.get("intent_kind") == "exit"


@pytest.mark.asyncio
async def test_validation_harness_urgent_exit_uses_fak_planner(tmp_path: Path) -> None:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy=_strategy_yaml(
            mode=VALIDATION_MODE_URGENT_EXIT,
            seed_allocation_qty="25",
            limit_price="0.50",
            use_fixture_book=True,
        ),
        runtime=_runtime(),
    )
    facts = tmp_path / "facts.jsonl"
    with JsonlSink(facts) as sink:
        await run_validation_harness_once(
            app=app,
            run_id=RunId(str(uuid4())),
            coord=_coord(tmp_path),
            sink=sink,
            oms=ShadowOMS(),
            cfg=app.validation_harness,
        )
    rows = _read(facts)
    plans = [r for r in rows if r["fact_type"] == FACT_TYPE_EXECUTION_PLAN]
    assert plans
    plan = plans[0]["payload"]
    assert plan.get("approved") is True
    assert plan.get("execution_style") == "FAK"
    assert plan.get("planner_reason") == "planner_urgent_exit_fak"
    assert any(r["fact_type"] == FACT_TYPE_OMS_SUBMIT for r in rows)


@pytest.mark.asyncio
async def test_validation_harness_stale_book_denies_without_oms_submit(tmp_path: Path) -> None:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy=_strategy_yaml(
            mode=VALIDATION_MODE_STALE_BOOK_DENY,
            seed_allocation_qty="25",
            limit_price="0.50",
            use_fixture_book=True,
        ),
        runtime=_runtime(),
    )
    facts = tmp_path / "facts.jsonl"
    with JsonlSink(facts) as sink:
        await run_validation_harness_once(
            app=app,
            run_id=RunId(str(uuid4())),
            coord=_coord(tmp_path),
            sink=sink,
            oms=ShadowOMS(),
            cfg=app.validation_harness,
        )
    rows = _read(facts)
    plans = [r for r in rows if r["fact_type"] == FACT_TYPE_EXECUTION_PLAN]
    assert plans
    assert plans[0]["payload"].get("approved") is False
    assert plans[0]["payload"].get("planner_reason") == "planner_stale_book"
    assert not any(r["fact_type"] == FACT_TYPE_OMS_SUBMIT for r in rows)


@pytest.mark.asyncio
async def test_validation_harness_registers_protection_after_allocation_buy_applied(
    tmp_path: Path,
) -> None:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy=_strategy_yaml(
            mode=VALIDATION_MODE_PROTECTION_REGISTER,
            protection_enabled=True,
        ),
        runtime=_runtime(),
    )
    facts = tmp_path / "facts.jsonl"
    run_id = RunId(str(uuid4()))
    with JsonlSink(facts) as sink:
        coord = _coord(tmp_path)
        _wire_reporting_sink(coord, sink, run_id)
        await run_validation_harness_once(
            app=app,
            run_id=run_id,
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=app.validation_harness,
        )
    rows = _read(facts)
    assert any(r["fact_type"] == FACT_TYPE_OMS_SUBMIT for r in rows)
    assert any(r["fact_type"] == FACT_TYPE_PROTECTION_REGISTER for r in rows)


@pytest.mark.asyncio
async def test_validation_harness_protection_tp_trigger_emits_exit_intent(tmp_path: Path) -> None:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy=_strategy_yaml(
            mode=VALIDATION_MODE_PROTECTION_TP,
            entry_price="0.40",
            seed_allocation_qty="25",
            use_fixture_book=True,
            protection_enabled=True,
        ),
        runtime=_runtime(),
    )
    facts = tmp_path / "facts.jsonl"
    with JsonlSink(facts) as sink:
        await run_validation_harness_once(
            app=app,
            run_id=RunId(str(uuid4())),
            coord=_coord(tmp_path),
            sink=sink,
            oms=ShadowOMS(),
            cfg=app.validation_harness,
        )
    rows = _read(facts)
    assert any(r["fact_type"] == FACT_TYPE_PROTECTION_TRIGGER for r in rows)
    assert any(r["fact_type"] == FACT_TYPE_OMS_SUBMIT for r in rows)
    triggers = [r for r in rows if r["fact_type"] == FACT_TYPE_PROTECTION_TRIGGER]
    assert triggers[0]["payload"].get("trigger") == "take_profit"


@pytest.mark.asyncio
async def test_validation_harness_protection_sl_trigger_emits_exit_intent(tmp_path: Path) -> None:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy=_strategy_yaml(
            mode=VALIDATION_MODE_PROTECTION_SL,
            entry_price="0.40",
            seed_allocation_qty="25",
            use_fixture_book=True,
            protection_enabled=True,
        ),
        runtime=_runtime(),
    )
    facts = tmp_path / "facts.jsonl"
    with JsonlSink(facts) as sink:
        await run_validation_harness_once(
            app=app,
            run_id=RunId(str(uuid4())),
            coord=_coord(tmp_path),
            sink=sink,
            oms=ShadowOMS(),
            cfg=app.validation_harness,
        )
    rows = _read(facts)
    assert any(r["fact_type"] == FACT_TYPE_PROTECTION_TRIGGER for r in rows)
    triggers = [r for r in rows if r["fact_type"] == FACT_TYPE_PROTECTION_TRIGGER]
    assert triggers[0]["payload"].get("trigger") == "stop_loss"


def test_validation_harness_all_sells_clamp_to_owner_allocation(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    assert coord.allocation_ledger is not None
    coord.allocation_ledger.apply_buy(
        "validation_harness",
        TokenId(TOKEN),
        Decimal("10"),
        correlation_id="seed",
    )
    strat = ValidationHarnessStrategy(owner_id="validation_harness")
    signal = ValidationSignal(
        token_id=TokenId(TOKEN),
        side=Side.SELL,
        order_style=app_order_style_gtc(),
        dedup_key="clamp-test",
        mode=VALIDATION_MODE_URGENT_EXIT,
        size=Decimal("50"),
        limit_price=Decimal("0.50"),
        urgency=URGENCY_URGENT,
    )
    from tyrex_pm.strategies.base import StrategyContext

    result = strat.on_signal(signal, StrategyContext(coord=coord))
    assert len(result.intents) == 1
    assert result.intents[0].size == Decimal("10")


def app_order_style_gtc():
    from tyrex_pm.core.enums import OrderStyle

    return OrderStyle.GTC
