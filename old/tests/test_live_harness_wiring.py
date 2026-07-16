"""Live validation harness policy tests (P4.5 wiring)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import ExecutionMode, Side
from tyrex_pm.core.errors import ConfigError
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import WalletPosition
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_EXECUTION_PLAN, FACT_TYPE_HEALTH
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import parse_app_config, ShadowBootstrapConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.validation_harness_live import validate_validation_harness_live_config
from tyrex_pm.runtime.validation_harness_run import run_validation_harness_once
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore, make_snapshot
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.core.time import utc_now

TOKEN = "token-live-harness"

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


def _shadow_runtime() -> dict:
    return {
        "execution_mode": "shadow",
        "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
        "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
        "supervisors": {"reconcile_interval_s": 30, "submit_grace_s": 15},
        "logging": {"level": "WARNING"},
        "market_data": {"enabled": True, "max_book_age_s": 5},
        "execution": {"planner": {"enabled": True, "require_fresh_book_for_urgent": True}},
    }


def _live_runtime() -> dict:
    rt = dict(_shadow_runtime())
    rt["execution_mode"] = "live"
    rt.pop("shadow_bootstrap", None)
    return rt


def _coord(tmp_path: Path) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    apply_shadow_bootstrap(
        coord.wallet,
        ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")),
    )
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    return coord


def test_live_config_rejects_seed_allocation_qty() -> None:
    with pytest.raises(ConfigError, match="seed_allocation_qty"):
        parse_app_config(
            risk=dict(_RISK),
            strategy={
                "kind": "validation_harness",
                "enabled": True,
                "token_id": TOKEN,
                "validation": {
                    "mode": "urgent_exit",
                    "token_id": TOKEN,
                    "seed_allocation_qty": "10",
                },
            },
            runtime=_live_runtime(),
        )


def test_live_config_rejects_use_fixture_book() -> None:
    with pytest.raises(ConfigError, match="use_fixture_book"):
        parse_app_config(
            risk=dict(_RISK),
            strategy={
                "kind": "validation_harness",
                "enabled": True,
                "token_id": TOKEN,
                "validation": {
                    "mode": "urgent_exit",
                    "token_id": TOKEN,
                    "use_fixture_book": True,
                },
            },
            runtime=_live_runtime(),
        )


def test_fixtures_not_used_in_live_unless_explicitly_allowed() -> None:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy={
            "kind": "validation_harness",
            "enabled": True,
            "token_id": TOKEN,
            "validation": {
                "mode": "urgent_exit",
                "token_id": TOKEN,
                "owner_id": "validation_harness",
                "use_fixture_book": False,
            },
        },
        runtime=_live_runtime(),
    )
    validate_validation_harness_live_config(app)
    assert app.runtime.execution_mode == ExecutionMode.LIVE
    assert app.validation_harness.use_fixture_book is False


@pytest.mark.asyncio
async def test_live_urgent_exit_does_not_inject_fixture_book(tmp_path: Path) -> None:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy={
            "kind": "validation_harness",
            "enabled": True,
            "token_id": TOKEN,
            "validation": {
                "mode": "urgent_exit",
                "token_id": TOKEN,
                "owner_id": "validation_harness",
                "limit_price": "0.50",
                "use_fixture_book": False,
            },
        },
        runtime=_live_runtime(),
    )
    coord = _coord(tmp_path)
    tid = TokenId(TOKEN)
    coord.allocation_ledger.apply_buy("validation_harness", tid, Decimal("25"), correlation_id="seed")
    coord.wallet.positions[tid] = WalletPosition(token_id=tid, qty=Decimal("25"), avg_price_usd=Decimal("0.5"))
    coord.market_state.apply_snapshot(
        make_snapshot(
            tid,
            bids=[(Decimal("0.49"), Decimal("1000"))],
            asks=[(Decimal("0.51"), Decimal("1000"))],
            ts=utc_now(),
        )
    )
    with patch(
        "tyrex_pm.runtime.validation_harness_run.inject_fixture_book",
        side_effect=AssertionError("inject_fixture_book must not be called in live"),
    ) as mock_inj:
        with patch(
            "tyrex_pm.runtime.validation_harness_run.process_signals",
            new_callable=AsyncMock,
        ) as mock_ps:
            with JsonlSink(tmp_path / "facts.jsonl") as sink:
                await run_validation_harness_once(
                    app=app,
                    run_id=RunId(str(uuid4())),
                    coord=coord,
                    sink=sink,
                    oms=ShadowOMS(),
                    cfg=app.validation_harness,
                    apply_local_shadow_fill=False,
                )
        mock_inj.assert_not_called()
        mock_ps.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_urgent_exit_uses_market_state_store(tmp_path: Path) -> None:
    """Live urgent exit must read planner book from MarketStateStore, not fixtures."""
    import json

    app = parse_app_config(
        risk=dict(_RISK),
        strategy={
            "kind": "validation_harness",
            "enabled": True,
            "token_id": TOKEN,
            "validation": {
                "mode": "urgent_exit",
                "token_id": TOKEN,
                "owner_id": "validation_harness",
                "limit_price": "0.50",
                "use_fixture_book": False,
            },
        },
        runtime=_live_runtime(),
    )
    coord = _coord(tmp_path)
    tid = TokenId(TOKEN)
    coord.allocation_ledger.apply_buy("validation_harness", tid, Decimal("25"), correlation_id="seed")
    coord.wallet.positions[tid] = WalletPosition(token_id=tid, qty=Decimal("25"), avg_price_usd=Decimal("0.5"))
    live_bid = Decimal("0.48")
    coord.market_state.apply_snapshot(
        make_snapshot(
            tid,
            bids=[(live_bid, Decimal("1000"))],
            asks=[(Decimal("0.52"), Decimal("1000"))],
            ts=utc_now(),
        )
    )
    coord.health.first_v2_sync_complete = True
    coord.health.heartbeat_ok = True
    coord.health.clob_session_ok = True
    facts = tmp_path / "facts.jsonl"
    with JsonlSink(facts) as sink:
        await run_validation_harness_once(
            app=app,
            run_id=RunId(str(uuid4())),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=app.validation_harness,
            apply_local_shadow_fill=True,
        )
    rows = [json.loads(x) for x in facts.read_text().splitlines() if x.strip()]
    plans = [r for r in rows if r["fact_type"] == FACT_TYPE_EXECUTION_PLAN]
    assert plans
    assert plans[0]["payload"].get("approved") is True
    evidence = plans[0]["payload"].get("evidence") or {}
    assert str(live_bid) in str(evidence) or evidence.get("best_bid") == str(live_bid)


@pytest.mark.asyncio
async def test_shadow_urgent_exit_can_use_fixture_book_explicitly(tmp_path: Path) -> None:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy={
            "kind": "validation_harness",
            "enabled": True,
            "token_id": TOKEN,
            "validation": {
                "mode": "urgent_exit",
                "token_id": TOKEN,
                "owner_id": "validation_harness",
                "limit_price": "0.50",
                "use_fixture_book": True,
                "seed_allocation_qty": "25",
                "allow_seed_allocation": True,
            },
        },
        runtime=_shadow_runtime(),
    )
    with patch(
        "tyrex_pm.runtime.validation_harness_run.inject_fixture_book",
    ) as mock_inj:
        with patch(
            "tyrex_pm.runtime.validation_harness_run.process_signals",
            new_callable=AsyncMock,
        ):
            with JsonlSink(tmp_path / "facts.jsonl") as sink:
                await run_validation_harness_once(
                    app=app,
                    run_id=RunId(str(uuid4())),
                    coord=_coord(tmp_path),
                    sink=sink,
                    oms=ShadowOMS(),
                    cfg=app.validation_harness,
                )
        mock_inj.assert_called_once()


@pytest.mark.asyncio
async def test_live_urgent_exit_denies_when_live_book_missing(tmp_path: Path) -> None:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy={
            "kind": "validation_harness",
            "enabled": True,
            "token_id": TOKEN,
            "validation": {
                "mode": "urgent_exit",
                "token_id": TOKEN,
                "owner_id": "validation_harness",
                "limit_price": "0.50",
            },
        },
        runtime=_live_runtime(),
    )
    coord = _coord(tmp_path)
    tid = TokenId(TOKEN)
    coord.allocation_ledger.apply_buy("validation_harness", tid, Decimal("25"), correlation_id="seed")
    coord.wallet.positions[tid] = WalletPosition(token_id=tid, qty=Decimal("25"), avg_price_usd=Decimal("0.5"))
    facts = tmp_path / "facts.jsonl"
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
    import json

    rows = [json.loads(x) for x in facts.read_text().splitlines() if x.strip()]
    preflight = [r for r in rows if r.get("payload", {}).get("event") == "validation_harness_preflight"]
    assert preflight and preflight[0]["payload"]["ok"] is True
    plans = [r for r in rows if r["fact_type"] == FACT_TYPE_EXECUTION_PLAN]
    if plans:
        assert plans[0]["payload"].get("approved") is False
