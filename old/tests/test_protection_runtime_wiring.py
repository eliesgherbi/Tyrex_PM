"""Protection runtime wiring tests (P4.5 architecture_enhance)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import ApprovedIntent, EnterIntent, WalletPosition
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.protection.config import SIZE_MODE_FULL
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_OMS_SUBMIT, FACT_TYPE_PROTECTION_REGISTER
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import ProtectionRuntimeConfig, parse_app_config, ShadowBootstrapConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.protection_runtime import (
    init_protection_monitor,
    maybe_register_protection_after_buy,
    run_protection_tick,
)
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore, make_snapshot
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.validation_harness.strategy import ValidationHarnessStrategy

TOKEN = TokenId("token-prot-runtime")

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


def _app(protection: bool = True):
    strat = {
        "kind": "validation_harness",
        "enabled": True,
        "token_id": str(TOKEN),
        "validation": {"token_id": str(TOKEN), "owner_id": "validation_harness"},
    }
    if protection:
        strat["protection"] = {
            "enabled": True,
            "take_profit_pct": "0.10",
            "stop_loss_pct": "0.05",
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
    from tyrex_pm.core.time import utc_now

    coord.market_state.apply_snapshot(
        make_snapshot(
            TOKEN,
            bids=[(Decimal("0.55"), Decimal("1000"))],
            asks=[(Decimal("0.57"), Decimal("1000"))],
            ts=utc_now(),
        )
    )
    return coord


def _read(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_protection_enabled_starts_monitor() -> None:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    assert getattr(coord, "protection_monitor", None) is None
    mon = init_protection_monitor(coord)
    assert mon is not None
    assert coord.protection_monitor is mon


def test_protection_register_does_not_register_on_matched_only(tmp_path: Path) -> None:
    app = _app()
    coord = _coord(tmp_path)
    init_protection_monitor(coord)
    facts = tmp_path / "facts.jsonl"
    strat = ValidationHarnessStrategy()
    intent = EnterIntent(
        token_id=TOKEN,
        side=Side.BUY,
        size=Decimal("25"),
        limit_price=Decimal("0.50"),
        order_style=OrderStyle.GTC,
    )
    ap = ApprovedIntent(intent=intent, client_order_id="cid-1", run_id=RunId(str(uuid4())))
    with JsonlSink(facts) as sink:
        coord.allocation_ledger_sink = sink
        coord.allocation_ledger_run_id = ap.run_id
        maybe_register_protection_after_buy(
            coord,
            app,
            strategy=strat,
            ap=ap,
            match_evidence={"match_status": "matched"},
            correlation_id="corr-1",
            intent_extensions={"allocation_owner_id": "validation_harness"},
            run_id=str(ap.run_id),
            apply_local_shadow_fill=False,
        )
    rows = _read(facts)
    assert not any(r["fact_type"] == FACT_TYPE_PROTECTION_REGISTER for r in rows)


@pytest.mark.asyncio
async def test_protection_register_waits_for_confirmed(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from tyrex_pm.core.models import TradeFillRecord
    from tyrex_pm.runtime.config import VALIDATION_MODE_PROTECTION_REGISTER
    from tyrex_pm.runtime.validation_harness_run import run_validation_harness_once

    app = parse_app_config(
        risk=dict(_RISK),
        strategy={
            "kind": "validation_harness",
            "enabled": True,
            "token_id": str(TOKEN),
            "validation": {
                "mode": VALIDATION_MODE_PROTECTION_REGISTER,
                "token_id": str(TOKEN),
                "owner_id": "validation_harness",
                "notional_usd": "5",
                "limit_price": "0.5",
                "wait_for_confirmed": True,
                "confirmed_timeout_s": 2,
            },
            "protection": {"enabled": True, "take_profit_pct": "0.1", "stop_loss_pct": "0.05"},
        },
        runtime=_runtime(),
    )
    coord = _coord(tmp_path)
    coord.wallet.trade_fill_records.append(
        TradeFillRecord(
            token_id=TOKEN,
            side=Side.BUY,
            size=Decimal("10"),
            price=Decimal("0.5"),
            status="CONFIRMED",
            ts_utc=datetime.now(timezone.utc),
        )
    )
    facts = tmp_path / "facts.jsonl"
    with patch(
        "tyrex_pm.runtime.validation_harness_run.process_signals",
        new_callable=AsyncMock,
    ):
        with JsonlSink(facts) as sink:
            await run_validation_harness_once(
                app=app,
                run_id=RunId(str(uuid4())),
                coord=coord,
                sink=sink,
                oms=ShadowOMS(),
                cfg=app.validation_harness,
                apply_local_shadow_fill=False,
            )
    rows = _read(facts)
    assert any(r.get("payload", {}).get("event") == "finality_wait" for r in rows)
    assert any(r["fact_type"] == FACT_TYPE_PROTECTION_REGISTER for r in rows)


@pytest.mark.asyncio
async def test_protection_register_emits_fact_after_finality(tmp_path: Path) -> None:
    """Alias coverage: protection_register fact only after allocation-final wait."""
    await test_protection_register_waits_for_confirmed(tmp_path)


def test_protection_registers_after_allocation_final_shadow(tmp_path: Path) -> None:
    app = _app()
    coord = _coord(tmp_path)
    init_protection_monitor(coord)
    facts = tmp_path / "facts.jsonl"
    strat = ValidationHarnessStrategy()
    intent = EnterIntent(
        token_id=TOKEN,
        side=Side.BUY,
        size=Decimal("25"),
        limit_price=Decimal("0.50"),
        order_style=OrderStyle.GTC,
    )
    ap = ApprovedIntent(intent=intent, client_order_id="cid-1", run_id=RunId(str(uuid4())))
    with JsonlSink(facts) as sink:
        coord.allocation_ledger_sink = sink
        coord.allocation_ledger_run_id = ap.run_id
        maybe_register_protection_after_buy(
            coord,
            app,
            strategy=strat,
            ap=ap,
            match_evidence={},
            correlation_id="corr-1",
            intent_extensions={"allocation_owner_id": "validation_harness"},
            run_id=str(ap.run_id),
            apply_local_shadow_fill=True,
        )
    rows = _read(facts)
    assert any(r["fact_type"] == FACT_TYPE_PROTECTION_REGISTER for r in rows)


@pytest.mark.asyncio
async def test_protection_monitor_routes_exit_intent_through_pipeline(tmp_path: Path) -> None:
    app = _app()
    coord = _coord(tmp_path)
    coord.allocation_ledger.apply_buy(
        "validation_harness", TOKEN, Decimal("25"), correlation_id="seed"
    )
    coord.wallet.positions[TOKEN] = WalletPosition(
        token_id=TOKEN, qty=Decimal("25"), avg_price_usd=Decimal("0.40")
    )
    mon = init_protection_monitor(coord)
    from tyrex_pm.protection.config import ProtectionPolicy

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
    facts = tmp_path / "facts.jsonl"
    strat = ValidationHarnessStrategy()
    with JsonlSink(facts) as sink:
        work = await run_protection_tick(
            coord,
            app,
            run_id=RunId(str(uuid4())),
            strategy=strat,
            sink=sink,
            oms=ShadowOMS(),
            apply_local_shadow_fill=True,
        )
    assert work
    rows = _read(facts)
    assert any(r["fact_type"] == FACT_TYPE_OMS_SUBMIT for r in rows)
