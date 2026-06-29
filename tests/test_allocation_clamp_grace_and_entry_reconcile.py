"""Regression tests for allocation clamp grace + paired entry reconciliation (Phase 4.6 fix)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import TradeFillRecord, WalletPosition
from tyrex_pm.core.time import utc_now
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.allocation_runtime import maybe_clamp_allocations_to_venue, maybe_repair_allocation_from_evidence
from tyrex_pm.runtime.config import ShadowBootstrapConfig, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.entry_qty_reconcile import reconcile_pair_entry_qty
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.paired_binary_run import _complete_entry_phase, _try_early_entry_completion
from tyrex_pm.state.allocation_ledger import (
    META_PROVISIONAL_UNTIL_TS,
    AllocationLedger,
    _entry_key,
)
from tyrex_pm.state.market_store import MarketStateStore
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from tyrex_pm.strategies.paired_binary.strategy import PairedBinaryStrategy

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"

_RISK = {
    "notional": {"min_usd": "0.01", "max_usd": "1000", "max_policy": "cap"},
    "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
    "venue_min_size": {"enabled": True, "default_min_size": "5"},
    "capital": {"enabled": False},
    "inventory": {"sell_requires_venue_position": False},
}


def _runtime(**over) -> dict:
    base = {
        "execution_mode": "shadow",
        "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
        "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
        "market_data": {"enabled": True, "max_book_age_s": 5},
        "execution": {"planner": {"enabled": True}},
        "allocation_ledger": {"clamp_grace_s_after_buy": 90},
    }
    base.update(over)
    return base


def _strategy(**pb_over) -> dict:
    pb = {
        "owner_id": "paired_binary",
        "market_id": "m1",
        "yes_token_id": YES,
        "no_token_id": NO,
        "position_size": "5",
        "max_pair_entry_cost": "1.02",
        "max_spread_yes": "0.02",
        "max_spread_no": "0.02",
        "entry_fill_timeout_s": 5,
        "min_effective_pair_qty": "5",
        "use_fixture_book": True,
        "fixture_yes_bid": "0.48",
        "fixture_yes_ask": "0.49",
        "fixture_no_bid": "0.50",
        "fixture_no_ask": "0.51",
        "run_once": True,
        "tick_interval_s": 0.05,
        "max_runtime_s": 5,
        "stop_after_entry": True,
    }
    pb.update(pb_over)
    return {"kind": "paired_binary", "enabled": True, "paired_binary": pb}


def _app(**pb_over):
    return parse_app_config(risk=dict(_RISK), strategy=_strategy(**pb_over), runtime=_runtime())


def _coord(tmp_path: Path, *, grace: float = 90.0) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(
        wallet=WalletStore(),
        orders=OrderStore(),
        health=HealthRuntime(),
        allocation_clamp_grace_s=grace,
    )
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    coord.allocation_ledger_run_id = str(uuid4())
    apply_shadow_bootstrap(
        coord.wallet,
        ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")),
    )
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    return coord


def test_recent_buy_allocation_not_clamped_to_zero_when_venue_lags(tmp_path: Path) -> None:
    ledger = AllocationLedger(path=tmp_path / "ledger.json")
    ledger.apply_buy("paired_binary", YES, Decimal("5"), correlation_id="y", clamp_grace_s=90)
    clamps, skipped = ledger.clamp_to_venue_positions({}, clamp_grace_s_after_buy=90)
    assert clamps == []
    assert len(skipped) == 1
    assert skipped[0].reason == "recent_buy_rest_lag"
    assert ledger.get_allocated("paired_binary", YES) == Decimal("5")


def test_old_allocation_still_clamped_when_venue_zero_after_grace(tmp_path: Path) -> None:
    ledger = AllocationLedger(path=tmp_path / "ledger.json")
    ledger.apply_buy("paired_binary", YES, Decimal("5"), correlation_id="y", clamp_grace_s=90)
    key = _entry_key("paired_binary", YES)
    entry = ledger._entries[key]
    entry.metadata[META_PROVISIONAL_UNTIL_TS] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    clamps, skipped = ledger.clamp_to_venue_positions({}, clamp_grace_s_after_buy=90)
    assert skipped == []
    assert len(clamps) == 1
    assert clamps[0].allocated_after == Decimal("0")
    assert ledger.get_allocated("paired_binary", YES) == Decimal("0")


def test_finality_confirmed_repairs_clamped_allocation(tmp_path: Path) -> None:
    app = _app()
    coord = _coord(tmp_path)
    ledger = coord.allocation_ledger
    assert ledger is not None
    ledger.apply_buy("paired_binary", TokenId(YES), Decimal("5"), correlation_id="y", clamp_grace_s=0)
    ledger.clamp_to_venue_positions({}, clamp_grace_s_after_buy=0)
    assert ledger.get_allocated("paired_binary", YES) == Decimal("0")
    coord.wallet.trade_fill_records.append(
        TradeFillRecord(
            token_id=TokenId(YES),
            side=Side.BUY,
            size=Decimal("5"),
            price=Decimal("0.49"),
            status="CONFIRMED",
            ts_utc=utc_now(),
        )
    )
    runs = tmp_path / "runs"
    runs.mkdir(parents=True)
    with JsonlSink(runs / "facts.jsonl") as sink:
        coord.allocation_ledger_sink = sink
        mut = maybe_repair_allocation_from_evidence(
            coord,
            app,
            owner_id="paired_binary",
            token_id=TokenId(YES),
            target_qty=Decimal("5"),
            source="user_ws",
            correlation_id="y:finality",
            run_id=str(uuid4()),
            reason="finality_confirmed",
        )
    assert mut is not None
    assert ledger.get_allocated("paired_binary", YES) == Decimal("5")
    lines = (runs / "facts.jsonl").read_text(encoding="utf-8").strip().splitlines()
    payload = json.loads(lines[-1])["payload"]
    assert payload["event"] == "allocation_repaired_from_finality"


@pytest.mark.asyncio
async def test_paired_entry_completes_when_finality_confirms_but_ledger_was_clamped(
    tmp_path: Path,
) -> None:
    app = _app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    ledger = coord.allocation_ledger
    assert ledger is not None
    ledger.apply_buy("paired_binary", TokenId(YES), Decimal("5"), correlation_id="y", clamp_grace_s=0)
    ledger.apply_buy("paired_binary", TokenId(NO), Decimal("5"), correlation_id="n", clamp_grace_s=0)
    ledger.clamp_to_venue_positions({}, clamp_grace_s_after_buy=0)
    assert ledger.get_available_allocated("paired_binary", TokenId(YES)) == Decimal("0")
    for tid, corr in ((YES, "y"), (NO, "n")):
        coord.wallet.trade_fill_records.append(
            TradeFillRecord(
                token_id=TokenId(tid),
                side=Side.BUY,
                size=Decimal("5"),
                price=Decimal("0.5"),
                status="CONFIRMED",
                ts_utc=utc_now(),
            )
        )
    from tyrex_pm.runtime.market_data_runtime import inject_fixture_book

    inject_fixture_book(coord, YES, best_bid=Decimal("0.48"), best_ask=Decimal("0.49"))
    inject_fixture_book(coord, NO, best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.BOTH_ENTRY_PENDING,
        pair_correlation_id="pc_fix",
        owner_id=cfg.owner_id,
        market_id=cfg.market_id,
        yes_token_id=cfg.yes_token_id,
        no_token_id=cfg.no_token_id,
        yes=__import__("tyrex_pm.strategies.paired_binary.state", fromlist=["LegRuntime"]).LegRuntime(
            leg_correlation_id="pc_fix:yes"
        ),
        no=__import__("tyrex_pm.strategies.paired_binary.state", fromlist=["LegRuntime"]).LegRuntime(
            leg_correlation_id="pc_fix:no"
        ),
    )
    runs = tmp_path / "runs"
    runs.mkdir(parents=True)
    with JsonlSink(runs / "facts.jsonl") as sink:
        coord.allocation_ledger_sink = sink
        ok = await _try_early_entry_completion(
            app=app,
            run_id=RunId(str(uuid4())),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            strategy=PairedBinaryStrategy(cfg),
            cfg=cfg,
            state=state,
            apply_local_shadow_fill=False,
            live_clob_client=None,
        )
    assert ok is True
    assert state.phase == PairedBinaryPhase.BOTH_LEGS_FILLED
    types = [json.loads(l)["fact_type"] for l in (runs / "facts.jsonl").read_text().splitlines()]
    assert "paired_binary_entry_qty_reconciled" in types
    assert "paired_binary_monitor_started" not in types


@pytest.mark.asyncio
async def test_paired_entry_timeout_unwinds_confirmed_single_leg(tmp_path: Path) -> None:
    app = _app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(YES), Decimal("5"), correlation_id="y")
    coord.wallet.positions[TokenId(YES)] = WalletPosition(
        token_id=TokenId(YES), qty=Decimal("5"), avg_price_usd=Decimal("0.49")
    )
    coord.wallet.trade_fill_records.append(
        TradeFillRecord(
            token_id=TokenId(YES),
            side=Side.BUY,
            size=Decimal("5"),
            price=Decimal("0.49"),
            status="CONFIRMED",
            ts_utc=utc_now(),
        )
    )
    from tyrex_pm.runtime.market_data_runtime import inject_fixture_book

    inject_fixture_book(coord, YES, best_bid=Decimal("0.48"), best_ask=Decimal("0.49"))
    inject_fixture_book(coord, NO, best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.BOTH_ENTRY_PENDING,
        pair_correlation_id="pc_unwind",
        owner_id=cfg.owner_id,
        market_id=cfg.market_id,
        yes_token_id=cfg.yes_token_id,
        no_token_id=cfg.no_token_id,
    )
    runs = tmp_path / "runs"
    runs.mkdir(parents=True)
    with JsonlSink(runs / "facts.jsonl") as sink:
        coord.allocation_ledger_sink = sink
        await _complete_entry_phase(
            app=app,
            run_id=RunId(str(uuid4())),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            strategy=PairedBinaryStrategy(cfg),
            cfg=cfg,
            state=state,
            apply_local_shadow_fill=True,
            live_clob_client=None,
            entry_timeout=True,
        )
    assert state.phase == PairedBinaryPhase.FAILED
    types = [json.loads(l)["fact_type"] for l in (runs / "facts.jsonl").read_text().splitlines()]
    assert "paired_binary_entry_timeout_unwind_retry" in types
    assert "paired_binary_emergency_unwind_started" in types or "paired_binary_unwind" in types


@pytest.mark.asyncio
async def test_paired_entry_timeout_does_not_fail_without_unwind_when_confirmed_qty_exists(
    tmp_path: Path,
) -> None:
    """Both legs CONFIRMED on WS but ledger clamped → repair and activate, not FAILED."""
    app = _app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    ledger = coord.allocation_ledger
    assert ledger is not None
    for tid, corr in ((YES, "y"), (NO, "n")):
        ledger.apply_buy("paired_binary", TokenId(tid), Decimal("5"), correlation_id=corr, clamp_grace_s=0)
        ledger.clamp_to_venue_positions({}, clamp_grace_s_after_buy=0)
        coord.wallet.trade_fill_records.append(
            TradeFillRecord(
                token_id=TokenId(tid),
                side=Side.BUY,
                size=Decimal("5"),
                price=Decimal("0.5"),
                status="CONFIRMED",
                ts_utc=utc_now(),
            )
        )
    from tyrex_pm.runtime.market_data_runtime import inject_fixture_book

    inject_fixture_book(coord, YES, best_bid=Decimal("0.48"), best_ask=Decimal("0.49"))
    inject_fixture_book(coord, NO, best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.BOTH_ENTRY_PENDING,
        pair_correlation_id="pc_both",
        owner_id=cfg.owner_id,
        market_id=cfg.market_id,
        yes_token_id=cfg.yes_token_id,
        no_token_id=cfg.no_token_id,
    )
    runs = tmp_path / "runs"
    runs.mkdir(parents=True)
    with JsonlSink(runs / "facts.jsonl") as sink:
        coord.allocation_ledger_sink = sink
        await _complete_entry_phase(
            app=app,
            run_id=RunId(str(uuid4())),
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            strategy=PairedBinaryStrategy(cfg),
            cfg=cfg,
            state=state,
            apply_local_shadow_fill=False,
            live_clob_client=None,
            entry_timeout=True,
        )
    assert state.phase == PairedBinaryPhase.BOTH_LEGS_FILLED


@pytest.mark.asyncio
async def test_paired_monitor_starts_after_reconciled_entry_qty(tmp_path: Path) -> None:
    app = _app(stop_after_entry=True)
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    run_id = RunId(str(uuid4()))
    runs = tmp_path / "runs" / str(run_id)
    runs.mkdir(parents=True)
    from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
    from tyrex_pm.strategies.paired_binary.state import PairedBinaryRuntimeState

    with JsonlSink(runs / "facts.jsonl") as sink:
        coord.allocation_ledger_sink = sink
        coord.allocation_ledger_run_id = str(run_id)
        state = PairedBinaryRuntimeState(
            owner_id=cfg.owner_id,
            market_id=cfg.market_id,
            yes_token_id=cfg.yes_token_id,
            no_token_id=cfg.no_token_id,
        )
        await run_paired_binary_loop(
            app=app,
            run_id=run_id,
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
            apply_local_shadow_fill=True,
        )
    assert state.phase == PairedBinaryPhase.BOTH_LEGS_ACTIVE
    types = [json.loads(l)["fact_type"] for l in (runs / "facts.jsonl").read_text().splitlines()]
    assert "paired_binary_monitor_started" in types


def test_reconciled_entry_qty_requires_min_effective_pair_qty(tmp_path: Path) -> None:
    app = _app(min_effective_pair_qty="10")
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(YES), Decimal("5"), correlation_id="y")
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(NO), Decimal("5"), correlation_id="n")
    coord.wallet.positions[TokenId(YES)] = WalletPosition(
        token_id=TokenId(YES), qty=Decimal("5"), avg_price_usd=Decimal("0.49")
    )
    coord.wallet.positions[TokenId(NO)] = WalletPosition(
        token_id=TokenId(NO), qty=Decimal("5"), avg_price_usd=Decimal("0.51")
    )
    pair = reconcile_pair_entry_qty(
        coord,
        app,
        owner_id=cfg.owner_id,
        yes_token_id=YES,
        no_token_id=NO,
        pair_correlation_id="pc",
        repair=False,
    )
    assert pair.effective_pair_qty == Decimal("5")


def test_sell_clamp_still_prevents_selling_more_than_venue_available(tmp_path: Path) -> None:
    from tyrex_pm.strategies.paired_binary.sizing import clamp_exit_size

    coord = _coord(tmp_path)
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(YES), Decimal("10"), correlation_id="y")
    coord.wallet.positions[TokenId(YES)] = WalletPosition(
        token_id=TokenId(YES), qty=Decimal("3"), avg_price_usd=Decimal("0.49")
    )
    sizing = clamp_exit_size(
        coord,
        owner_id="paired_binary",
        token_id=TokenId(YES),
        planned=Decimal("10"),
        exit_order_style=OrderStyle.FAK,
    )
    assert sizing.final_size == Decimal("3")


def test_live_preflight_rejects_fixture_book_and_seed_allocation() -> None:
    from tyrex_pm.core.errors import ConfigError

    with pytest.raises(ConfigError, match="fixture"):
        parse_app_config(
            risk=dict(_RISK),
            strategy=_strategy(use_fixture_book=True),
            runtime=_runtime(execution_mode="live"),
        )
    with pytest.raises(ConfigError, match="seed"):
        parse_app_config(
            risk=dict(_RISK),
            strategy=_strategy(use_fixture_book=False, allow_seed_allocation=True, seed_allocation_qty="5"),
            runtime=_runtime(execution_mode="live"),
        )


def test_maybe_clamp_emits_skip_fact(tmp_path: Path) -> None:
    app = _app()
    coord = _coord(tmp_path)
    coord.allocation_ledger.apply_buy("paired_binary", YES, Decimal("5"), correlation_id="y", clamp_grace_s=90)
    runs = tmp_path / "runs"
    runs.mkdir(parents=True)
    with JsonlSink(runs / "facts.jsonl") as sink:
        coord.allocation_ledger_sink = sink
        maybe_clamp_allocations_to_venue(coord, run_id=str(uuid4()))
    payload = json.loads((runs / "facts.jsonl").read_text().strip().splitlines()[-1])["payload"]
    assert payload["event"] == "allocation_clamp_skipped_recent_buy"
