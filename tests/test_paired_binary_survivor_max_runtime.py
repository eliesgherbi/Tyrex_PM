"""Paired binary max_runtime survivor policy tests."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import ExecutionMode, OrderStyle
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import WalletPosition
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_PAIRED_BINARY_OPEN_EXPOSURE_AT_SHUTDOWN,
    FACT_TYPE_PAIRED_BINARY_SHUTDOWN_RUNTIME_EXTENSION,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import (
    SURVIVOR_ON_MAX_RUNTIME_CONTINUE,
    SURVIVOR_ON_MAX_RUNTIME_FORCE,
    SURVIVOR_ON_MAX_RUNTIME_MANUAL,
    parse_app_config,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore, make_snapshot
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.runtime.config import ShadowBootstrapConfig
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.exit_engine import ensure_pnl_budgets
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"


def _risk():
    return {
        "notional": {"min_usd": "0.01", "max_usd": "1000", "max_policy": "cap"},
        "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
        "venue_min_size": {"enabled": False},
        "capital": {"enabled": False},
        "inventory": {"sell_requires_venue_position": False},
        "concurrency": {"max_orders_in_flight": 10},
        "readiness": {"require_wallet_sync": False},
    }


def _strategy(**pb_over):
    pb = {
        "owner_id": "paired_binary",
        "market_id": "m1",
        "yes_token_id": YES,
        "no_token_id": NO,
        "position_size": "5",
        "max_pair_entry_cost": "1.02",
        "max_spread_yes": "0.05",
        "max_spread_no": "0.05",
        "pair_stop_loss_pct": "0.04",
        "pair_take_profit_pct": "0.10",
        "slippage_buffer": "0.005",
        "reject_if_spread_exceeds_loss_budget": False,
        "max_holding_time_s": 3600,
        "entry_fill_timeout_s": 5,
        "min_effective_pair_qty": "5",
        "use_fixture_book": True,
        "fixture_yes_bid": "0.48",
        "fixture_yes_ask": "0.49",
        "fixture_no_bid": "0.50",
        "fixture_no_ask": "0.51",
        "tick_interval_s": 0.01,
        "max_runtime_s": 0.05,
    }
    pb.update(pb_over)
    return {"kind": "paired_binary", "enabled": True, "paired_binary": pb}


def _app(survivor_policy: str = SURVIVOR_ON_MAX_RUNTIME_FORCE, **pb_over):
    return parse_app_config(
        risk=_risk(),
        strategy=_strategy(**pb_over),
        runtime={
            "execution_mode": "shadow",
            "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
            "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
            "market_data": {"enabled": True, "max_book_age_s": 5},
            "execution": {"planner": {"enabled": True}},
            "observability": {"emit_decision_snapshot": False},
            "paired_binary": {
                "poll_interval_s": 0.005,
                "survivor_on_max_runtime": survivor_policy,
                "survivor_timeout_s": 0.05,
            },
        },
    )


def _coord(tmp_path: Path) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    apply_shadow_bootstrap(
        coord.wallet,
        ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")),
    )
    store = MarketStateStore(default_max_age_s=5.0)
    store.apply_snapshot(
        make_snapshot(TokenId(YES), bids=[(Decimal("0.48"), Decimal("100"))], asks=[(Decimal("0.49"), Decimal("100"))])
    )
    store.apply_snapshot(
        make_snapshot(TokenId(NO), bids=[(Decimal("0.54"), Decimal("100"))], asks=[(Decimal("0.57"), Decimal("100"))])
    )
    coord.market_state = store
    coord.wallet.positions[TokenId(NO)] = WalletPosition(
        token_id=TokenId(NO), qty=Decimal("5"), avg_price_usd=Decimal("0.47")
    )
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(NO), Decimal("5"), correlation_id="seed-n")
    return coord


def _only_no_state() -> PairedBinaryRuntimeState:
    st = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.ONLY_NO_ACTIVE,
        pair_correlation_id="pc_live3",
        owner_id="paired_binary",
        market_id="m1",
        yes_token_id=YES,
        no_token_id=NO,
        yes_entry=Decimal("0.54"),
        no_entry=Decimal("0.47"),
        effective_qty=Decimal("5"),
        pair_opened_ts=monotonic_s(),
        activation_ts=monotonic_s(),
    )
    ensure_pnl_budgets(st, _app().paired_binary)  # type: ignore[arg-type]
    return st


def _facts_path(sink: JsonlSink) -> Path:
    return sink._path


def _facts(sink: JsonlSink) -> list[dict]:
    path = _facts_path(sink)
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.mark.asyncio
async def test_idle_max_runtime_stops_normally(tmp_path: Path) -> None:
    app = _app(max_runtime_s=0.02, entry_dry_run=True)
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    state = PairedBinaryRuntimeState(phase=PairedBinaryPhase.IDLE)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        oms = ShadowOMS()
        ticks = await run_paired_binary_loop(
            app=app,
            run_id=RunId("idle-stop"),
            coord=coord,
            sink=sink,
            oms=oms,
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )
    assert ticks >= 0
    assert state.phase == PairedBinaryPhase.IDLE
    types = {f["fact_type"] for f in _facts(sink)}
    assert FACT_TYPE_PAIRED_BINARY_OPEN_EXPOSURE_AT_SHUTDOWN not in types


@pytest.mark.asyncio
async def test_only_no_active_force_exit_on_max_runtime(tmp_path: Path) -> None:
    app = _app(survivor_policy=SURVIVOR_ON_MAX_RUNTIME_FORCE, max_runtime_s=0.01, tick_interval_s=0.005)
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    state = _only_no_state()
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        oms = ShadowOMS()
        await run_paired_binary_loop(
            app=app,
            run_id=RunId("live3-regression"),
            coord=coord,
            sink=sink,
            oms=oms,
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )

    facts = _facts(sink)
    types = [f["fact_type"] for f in facts]
    assert FACT_TYPE_PAIRED_BINARY_OPEN_EXPOSURE_AT_SHUTDOWN in types
    assert state.phase in {PairedBinaryPhase.DONE, PairedBinaryPhase.FAILED}
    assert state.phase != PairedBinaryPhase.ONLY_NO_ACTIVE


@pytest.mark.asyncio
async def test_manual_intervention_policy_emits_fact(tmp_path: Path) -> None:
    app = _app(survivor_policy=SURVIVOR_ON_MAX_RUNTIME_MANUAL, max_runtime_s=0.01, tick_interval_s=0.005)
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    state = _only_no_state()
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        oms = ShadowOMS()
        await run_paired_binary_loop(
            app=app,
            run_id=RunId("manual-survivor"),
            coord=coord,
            sink=sink,
            oms=oms,
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
        )
    assert state.phase == PairedBinaryPhase.FAILED
    facts = _facts(sink)
    assert any(f["fact_type"] == "paired_binary_manual_intervention_required" for f in facts)


@pytest.mark.asyncio
async def test_continue_policy_emits_extension_fact(tmp_path: Path) -> None:
    app = _app(
        survivor_policy=SURVIVOR_ON_MAX_RUNTIME_CONTINUE,
        max_runtime_s=0.02,
        tick_interval_s=0.005,
    )
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    state = _only_no_state()

    t0 = monotonic_s()
    call_count = 0

    def fake_mono():
        nonlocal call_count
        call_count += 1
        if call_count < 5:
            return t0
        if call_count < 15:
            return t0 + 0.03
        return t0 + 0.2

    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        oms = ShadowOMS()
        with patch("tyrex_pm.runtime.paired_binary_run.monotonic_s", side_effect=fake_mono):
            await run_paired_binary_loop(
                app=app,
                run_id=RunId("extend-survivor"),
                coord=coord,
                sink=sink,
                oms=oms,
                cfg=cfg,
                state=state,
                state_dir=tmp_path,
            )

    types = {f["fact_type"] for f in _facts(sink)}
    assert FACT_TYPE_PAIRED_BINARY_SHUTDOWN_RUNTIME_EXTENSION in types
