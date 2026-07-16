"""Runtime integration tests for simple_signal_test CLI path (P1 runtime glue)."""

from __future__ import annotations

import argparse
import asyncio
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from tyrex_pm.core.ids import RunId
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_HEALTH,
    FACT_TYPE_INTENT,
    FACT_TYPE_OMS_SUBMIT,
    FACT_TYPE_RISK,
    FACT_TYPE_SIGNAL_RECEIVED,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import (
    STRATEGY_KIND_GURU_FOLLOW,
    STRATEGY_KIND_SIMPLE_SIGNAL_TEST,
    parse_app_config,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.fixture_signal_run import run_fixture_signals_once
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.runtime.config import ShadowBootstrapConfig

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
}

_RUNTIME = {
    "execution_mode": "shadow",
    "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
    "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
    "supervisors": {"reconcile_interval_s": 30, "submit_grace_s": 15},
    "logging": {"level": "WARNING"},
}


def _simple_strategy_yaml() -> dict:
    return {
        "kind": "simple_signal_test",
        "enabled": True,
        "token_id": "token-cli-test",
        "owner_id": "simple_signal_test",
        "side": "BUY",
        "notional_usd": "5",
        "limit_price": "0.5",
        "order_style": "GTC",
        "run_once": True,
    }


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
async def test_simple_signal_test_runtime_uses_process_signals(tmp_path: Path) -> None:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy=_simple_strategy_yaml(),
        runtime=dict(_RUNTIME),
    )
    assert app.strategy_kind == STRATEGY_KIND_SIMPLE_SIGNAL_TEST
    facts = tmp_path / "facts.jsonl"
    with patch("tyrex_pm.runtime.fixture_signal_run.process_signals", new_callable=AsyncMock) as mock_ps:
        with JsonlSink(facts) as sink:
            n = await run_fixture_signals_once(
                app=app,
                run_id=RunId(str(uuid4())),
                coord=_coord(tmp_path),
                sink=sink,
                oms=ShadowOMS(),
                cfg=app.simple_signal_test,
            )
        assert n == 1
        mock_ps.assert_awaited_once()


@pytest.mark.asyncio
async def test_simple_signal_test_emits_signal_received_from_cli_path(tmp_path: Path) -> None:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy=_simple_strategy_yaml(),
        runtime=dict(_RUNTIME),
    )
    assert app.simple_signal_test is not None
    facts = tmp_path / "facts.jsonl"
    with JsonlSink(facts) as sink:
        n = await run_fixture_signals_once(
            app=app,
            run_id=RunId(str(uuid4())),
            coord=_coord(tmp_path),
            sink=sink,
            oms=ShadowOMS(),
            cfg=app.simple_signal_test,
        )
    assert n == 1
    rows = _read(facts)
    assert any(r["fact_type"] == FACT_TYPE_SIGNAL_RECEIVED for r in rows)
    assert any(r["fact_type"] == FACT_TYPE_INTENT for r in rows)
    assert any(r["fact_type"] == FACT_TYPE_RISK for r in rows)
    assert any(r["fact_type"] == FACT_TYPE_OMS_SUBMIT for r in rows)


@pytest.mark.asyncio
async def test_simple_signal_test_exits_cleanly(tmp_path: Path) -> None:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy={**_simple_strategy_yaml(), "enabled": False},
        runtime=dict(_RUNTIME),
    )
    assert app.simple_signal_test is not None
    facts = tmp_path / "facts.jsonl"
    with JsonlSink(facts) as sink:
        n = await run_fixture_signals_once(
            app=app,
            run_id=RunId(str(uuid4())),
            coord=_coord(tmp_path),
            sink=sink,
            oms=ShadowOMS(),
            cfg=app.simple_signal_test,
        )
    assert n == 0
    assert _read(facts) == []


@pytest.mark.asyncio
async def test_simple_signal_test_cli_does_not_poll_guru(tmp_path: Path, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    poll_mock = AsyncMock(side_effect=AssertionError("poll_guru_incremental must not be called"))
    monkeypatch.setattr("tyrex_pm.runtime.app.poll_guru_incremental", poll_mock)

    args = argparse.Namespace(
        repo_root=root,
        strategy=str(root / "config" / "strategies" / "simple_signal_test.yaml"),
        scenario=None,
        state_dir=str(tmp_path / "state"),
        once=True,
        fixture=None,
        max_iterations=None,
        run_name="cli_test",
    )
    from tyrex_pm.runtime import app as app_mod

    await app_mod.cmd_run(args)
    poll_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_simple_signal_test_does_not_require_guru_wallet(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    import logging

    caplog.set_level(logging.WARNING)
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(
        "tyrex_pm.runtime.app.poll_guru_incremental",
        AsyncMock(side_effect=AssertionError("poll_guru_incremental must not be called")),
    )
    args = argparse.Namespace(
        repo_root=root,
        strategy=str(root / "config" / "strategies" / "simple_signal_test.yaml"),
        scenario=None,
        state_dir=str(tmp_path / "state"),
        once=True,
        fixture=None,
        max_iterations=None,
        run_name="cli_no_guru_warn",
    )
    from tyrex_pm.runtime import app as app_mod

    await app_mod.cmd_run(args)
    assert not any("guru.wallet is unset" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_non_guru_strategy_never_calls_poll_guru_incremental(tmp_path: Path, monkeypatch) -> None:
    """Alias guard: simple_signal_test path must not touch guru ingestion."""
    await test_simple_signal_test_cli_does_not_poll_guru(tmp_path, monkeypatch)


def test_unknown_strategy_kind_does_not_fallback_to_guru() -> None:
    from dataclasses import replace

    app = parse_app_config(
        risk=dict(_RISK),
        strategy=_simple_strategy_yaml(),
        runtime=dict(_RUNTIME),
    )
    # Simulate a future valid config kind that forgot to wire app.py main loop.
    bogus = replace(app, strategy_kind="future_kind")
    from tyrex_pm.runtime.app import _RUNTIME_WIRED_STRATEGY_KINDS

    assert "future_kind" not in _RUNTIME_WIRED_STRATEGY_KINDS

    with pytest.raises(RuntimeError, match="no runtime loop wired"):
        if bogus.strategy_kind not in _RUNTIME_WIRED_STRATEGY_KINDS:
            raise RuntimeError(
                f"strategy kind {bogus.strategy_kind!r} has no runtime loop wired in runtime/app.py"
            )


def test_guru_follow_still_has_strategy_kind() -> None:
    root = Path(__file__).resolve().parents[1]
    from tyrex_pm.runtime.config import load_app_config

    app = load_app_config(repo_root=root, scenario_file="shadow_guru")
    assert app.strategy_kind == STRATEGY_KIND_GURU_FOLLOW
