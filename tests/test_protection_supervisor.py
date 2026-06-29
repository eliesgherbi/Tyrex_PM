"""ProtectionSupervisor tests (P4.5 live wiring)."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import OrderStyle
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import WalletPosition
from tyrex_pm.core.time import utc_now
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.protection.config import ProtectionPolicy, SIZE_MODE_FULL
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import (
    ProtectionRuntimeConfig,
    ShadowBootstrapConfig,
    parse_app_config,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.protection_runtime import init_protection_monitor
from tyrex_pm.runtime.protection_supervisor import run_protection_supervisor
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore, make_snapshot
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.validation_harness.strategy import ValidationHarnessStrategy

TOKEN = TokenId("tok-supervisor")

_RISK = {
    "notional": {"min_usd": "0.01", "max_usd": "1000", "max_policy": "cap"},
    "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
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


def _runtime() -> dict:
    return {
        "execution_mode": "shadow",
        "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
        "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
        "supervisors": {"reconcile_interval_s": 30, "submit_grace_s": 15},
        "logging": {"level": "WARNING"},
        "market_data": {"enabled": True, "max_book_age_s": 5},
        "execution": {"planner": {"enabled": True}},
    }


def _app():
    strat = {
        "kind": "validation_harness",
        "enabled": True,
        "token_id": str(TOKEN),
        "validation": {"token_id": str(TOKEN), "owner_id": "validation_harness"},
        "protection": {
            "enabled": True,
            "take_profit_pct": "0.10",
            "stop_loss_pct": "0.10",
            "tick_interval_s": 0.05,
            "max_runtime_s": 0.5,
            "fail_if_no_trigger": False,
        },
    }
    return parse_app_config(risk=dict(_RISK), strategy=strat, runtime=_runtime())


def _coord(tmp_path: Path) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    apply_shadow_bootstrap(
        coord.wallet,
        ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")),
    )
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    return coord


@pytest.mark.asyncio
async def test_protection_supervisor_ticks_until_trigger(tmp_path: Path) -> None:
    app = _app()
    coord = _coord(tmp_path)
    coord.allocation_ledger.apply_buy("validation_harness", TOKEN, Decimal("25"), correlation_id="seed")
    coord.wallet.positions[TOKEN] = WalletPosition(
        token_id=TOKEN, qty=Decimal("25"), avg_price_usd=Decimal("0.40")
    )
    mon = init_protection_monitor(coord)
    mon.register(
        owner_id="validation_harness",
        token_id=TOKEN,
        entry_price=Decimal("0.40"),
        policy=ProtectionPolicy(
            take_profit_pct=Decimal("0.10"),
            stop_loss_pct=Decimal("0.10"),
            size_mode=SIZE_MODE_FULL,
            exit_order_style=OrderStyle.FAK,
            max_book_age_s=5.0,
        ),
        parent_correlation_id="seed",
        sink=None,
        run_id=RunId(str(uuid4())),
    )
    coord.market_state.apply_snapshot(
        make_snapshot(
            TOKEN,
            bids=[(Decimal("0.55"), Decimal("1000"))],
            asks=[(Decimal("0.57"), Decimal("1000"))],
            ts=utc_now(),
        )
    )
    facts = tmp_path / "facts.jsonl"
    with JsonlSink(facts) as sink:
        result = await run_protection_supervisor(
            coord,
            app,
            run_id=RunId(str(uuid4())),
            strategy=ValidationHarnessStrategy(),
            sink=sink,
            oms=ShadowOMS(),
            apply_local_shadow_fill=True,
        )
    assert result.ticks >= 1
    assert result.triggered is True


@pytest.mark.asyncio
async def test_protection_supervisor_timeout_no_trigger(tmp_path: Path) -> None:
    app = _app()
    coord = _coord(tmp_path)
    mon = init_protection_monitor(coord)
    mon.register(
        owner_id="validation_harness",
        token_id=TOKEN,
        entry_price=Decimal("0.40"),
        policy=ProtectionPolicy(
            take_profit_pct=Decimal("0.50"),
            stop_loss_pct=Decimal("0.50"),
            size_mode=SIZE_MODE_FULL,
            exit_order_style=OrderStyle.FAK,
            max_book_age_s=5.0,
        ),
        parent_correlation_id="seed",
        sink=None,
        run_id=RunId(str(uuid4())),
    )
    coord.market_state.apply_snapshot(
        make_snapshot(
            TOKEN,
            bids=[(Decimal("0.39"), Decimal("1000"))],
            asks=[(Decimal("0.41"), Decimal("1000"))],
            ts=utc_now(),
        )
    )
    facts = tmp_path / "facts.jsonl"
    prot = ProtectionRuntimeConfig(
        enabled=True,
        take_profit_pct=Decimal("0.50"),
        stop_loss_pct=Decimal("0.50"),
        tick_interval_s=0.05,
        max_runtime_s=0.2,
        fail_if_no_trigger=False,
    )
    with JsonlSink(facts) as sink:
        result = await run_protection_supervisor(
            coord,
            app,
            run_id=RunId(str(uuid4())),
            strategy=ValidationHarnessStrategy(),
            sink=sink,
            oms=ShadowOMS(),
            prot=prot,
        )
    assert result.triggered is False
    assert result.stopped_reason in ("max_runtime", "timeout_no_trigger")


@pytest.mark.asyncio
async def test_protection_supervisor_routes_exit_intent_through_pipeline(tmp_path: Path) -> None:
    from tyrex_pm.reporting.schema_v2 import FACT_TYPE_OMS_SUBMIT

    app = _app()
    coord = _coord(tmp_path)
    coord.allocation_ledger.apply_buy("validation_harness", TOKEN, Decimal("25"), correlation_id="seed")
    coord.wallet.positions[TOKEN] = WalletPosition(
        token_id=TOKEN, qty=Decimal("25"), avg_price_usd=Decimal("0.40")
    )
    mon = init_protection_monitor(coord)
    mon.register(
        owner_id="validation_harness",
        token_id=TOKEN,
        entry_price=Decimal("0.40"),
        policy=ProtectionPolicy(
            take_profit_pct=Decimal("0.10"),
            stop_loss_pct=Decimal("0.10"),
            size_mode=SIZE_MODE_FULL,
            exit_order_style=OrderStyle.FAK,
            max_book_age_s=5.0,
        ),
        parent_correlation_id="seed",
        sink=None,
        run_id=RunId(str(uuid4())),
    )
    coord.market_state.apply_snapshot(
        make_snapshot(
            TOKEN,
            bids=[(Decimal("0.55"), Decimal("1000"))],
            asks=[(Decimal("0.57"), Decimal("1000"))],
            ts=utc_now(),
        )
    )
    facts = tmp_path / "facts.jsonl"
    with JsonlSink(facts) as sink:
        result = await run_protection_supervisor(
            coord,
            app,
            run_id=RunId(str(uuid4())),
            strategy=ValidationHarnessStrategy(),
            sink=sink,
            oms=ShadowOMS(),
            apply_local_shadow_fill=True,
        )
    assert result.triggered is True
    rows = [json.loads(x) for x in facts.read_text().splitlines() if x.strip()]
    assert any(r["fact_type"] == FACT_TYPE_OMS_SUBMIT for r in rows)


@pytest.mark.asyncio
async def test_protection_supervisor_dedupes_triggers(tmp_path: Path) -> None:
    from tyrex_pm.reporting.schema_v2 import FACT_TYPE_PROTECTION_TRIGGER

    app = _app()
    prot = ProtectionRuntimeConfig(
        enabled=True,
        take_profit_pct=Decimal("0.10"),
        stop_loss_pct=Decimal("0.10"),
        tick_interval_s=0.05,
        max_runtime_s=0.35,
        stop_after_trigger=False,
        stop_after_exit_submit=False,
        fail_if_no_trigger=False,
    )
    coord = _coord(tmp_path)
    coord.allocation_ledger.apply_buy("validation_harness", TOKEN, Decimal("25"), correlation_id="seed")
    coord.wallet.positions[TOKEN] = WalletPosition(
        token_id=TOKEN, qty=Decimal("25"), avg_price_usd=Decimal("0.40")
    )
    mon = init_protection_monitor(coord)
    mon.register(
        owner_id="validation_harness",
        token_id=TOKEN,
        entry_price=Decimal("0.40"),
        policy=ProtectionPolicy(
            take_profit_pct=Decimal("0.10"),
            stop_loss_pct=Decimal("0.10"),
            size_mode=SIZE_MODE_FULL,
            exit_order_style=OrderStyle.FAK,
            max_book_age_s=5.0,
        ),
        parent_correlation_id="seed",
        sink=None,
        run_id=RunId(str(uuid4())),
    )
    coord.market_state.apply_snapshot(
        make_snapshot(
            TOKEN,
            bids=[(Decimal("0.55"), Decimal("1000"))],
            asks=[(Decimal("0.57"), Decimal("1000"))],
            ts=utc_now(),
        )
    )
    facts = tmp_path / "facts.jsonl"
    with JsonlSink(facts) as sink:
        await run_protection_supervisor(
            coord,
            app,
            run_id=RunId(str(uuid4())),
            strategy=ValidationHarnessStrategy(),
            sink=sink,
            oms=ShadowOMS(),
            prot=prot,
            apply_local_shadow_fill=True,
        )
    rows = [json.loads(x) for x in facts.read_text().splitlines() if x.strip()]
    triggers = [r for r in rows if r["fact_type"] == FACT_TYPE_PROTECTION_TRIGGER]
    assert len(triggers) == 1


@pytest.mark.asyncio
async def test_protection_supervisor_stops_after_exit_submit(tmp_path: Path) -> None:
    app = _app()
    coord = _coord(tmp_path)
    coord.allocation_ledger.apply_buy("validation_harness", TOKEN, Decimal("25"), correlation_id="seed")
    coord.wallet.positions[TOKEN] = WalletPosition(
        token_id=TOKEN, qty=Decimal("25"), avg_price_usd=Decimal("0.40")
    )
    mon = init_protection_monitor(coord)
    mon.register(
        owner_id="validation_harness",
        token_id=TOKEN,
        entry_price=Decimal("0.40"),
        policy=ProtectionPolicy(
            take_profit_pct=Decimal("0.10"),
            stop_loss_pct=Decimal("0.10"),
            size_mode=SIZE_MODE_FULL,
            exit_order_style=OrderStyle.FAK,
            max_book_age_s=5.0,
        ),
        parent_correlation_id="seed",
        sink=None,
        run_id=RunId(str(uuid4())),
    )
    coord.market_state.apply_snapshot(
        make_snapshot(
            TOKEN,
            bids=[(Decimal("0.55"), Decimal("1000"))],
            asks=[(Decimal("0.57"), Decimal("1000"))],
            ts=utc_now(),
        )
    )
    facts = tmp_path / "facts.jsonl"
    with JsonlSink(facts) as sink:
        result = await run_protection_supervisor(
            coord,
            app,
            run_id=RunId(str(uuid4())),
            strategy=ValidationHarnessStrategy(),
            sink=sink,
            oms=ShadowOMS(),
            apply_local_shadow_fill=True,
        )
    assert result.exit_submitted is True
    assert result.stopped_reason == "exit_submit"


@pytest.mark.asyncio
async def test_protection_supervisor_uses_live_market_state_not_fixture(tmp_path: Path) -> None:
    """Supervisor ticks against coordinator MarketStateStore (no fixture injection path)."""
    app = _app()
    coord = _coord(tmp_path)
    mon = init_protection_monitor(coord)
    mon.register(
        owner_id="validation_harness",
        token_id=TOKEN,
        entry_price=Decimal("0.40"),
        policy=ProtectionPolicy(
            take_profit_pct=Decimal("0.50"),
            stop_loss_pct=Decimal("0.50"),
            size_mode=SIZE_MODE_FULL,
            exit_order_style=OrderStyle.FAK,
            max_book_age_s=5.0,
        ),
        parent_correlation_id="seed",
        sink=None,
        run_id=RunId(str(uuid4())),
    )
    coord.market_state.apply_snapshot(
        make_snapshot(
            TOKEN,
            bids=[(Decimal("0.39"), Decimal("1000"))],
            asks=[(Decimal("0.41"), Decimal("1000"))],
            ts=utc_now(),
        )
    )
    facts = tmp_path / "facts.jsonl"
    with JsonlSink(facts) as sink:
        result = await run_protection_supervisor(
            coord,
            app,
            run_id=RunId(str(uuid4())),
            strategy=ValidationHarnessStrategy(),
            sink=sink,
            oms=ShadowOMS(),
            prot=ProtectionRuntimeConfig(
                enabled=True,
                take_profit_pct=Decimal("0.50"),
                stop_loss_pct=Decimal("0.50"),
                tick_interval_s=0.05,
                max_runtime_s=0.15,
                fail_if_no_trigger=False,
            ),
        )
    assert result.ticks >= 1
    rows = [json.loads(x) for x in facts.read_text().splitlines() if x.strip()]
    ticks = [r for r in rows if r.get("fact_type") == "protection_tick"]
    assert ticks
