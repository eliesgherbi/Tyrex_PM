"""Paired binary monitor tests (Phase 4.6)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import ExitIntent, WalletPosition
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import PairedBinaryStrategyConfig, ShadowBootstrapConfig, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore, make_snapshot
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.exit_engine import confirm_exit_submitted, ensure_pnl_budgets
from tyrex_pm.strategies.paired_binary.monitor import PairedBinaryMonitor
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"

_RISK = {
    "notional": {"min_usd": "0.01", "max_usd": "1000", "max_policy": "cap"},
    "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
    "venue_min_size": {"enabled": False},
    "capital": {"enabled": False},
    "inventory": {"sell_requires_venue_position": False},
}


def _cfg(**over) -> PairedBinaryStrategyConfig:
    base = dict(
        enabled=True,
        owner_id="paired_binary",
        market_id="m1",
        yes_token_id=YES,
        no_token_id=NO,
        position_size=Decimal("5"),
        max_pair_entry_cost=Decimal("1.02"),
        max_spread_yes=Decimal("0.02"),
        max_spread_no=Decimal("0.02"),
        pair_stop_loss_pct=Decimal("0.04"),
        pair_take_profit_pct=Decimal("0.10"),
        slippage_buffer=Decimal("0.005"),
        reject_if_spread_exceeds_loss_budget=True,
        max_holding_time_s=3600.0,
        entry_order_style=OrderStyle.GTC,
        exit_order_style=OrderStyle.FAK,
        entry_fill_timeout_s=60.0,
        abort_unpaired_entry=True,
        unwind_partial_entry=True,
        min_effective_pair_qty=Decimal("5"),
        run_once=True,
        max_markets=1,
        tick_interval_s=0.1,
        max_book_age_s=5.0,
    )
    base.update(over)
    return PairedBinaryStrategyConfig(**base)


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
        make_snapshot(TokenId(NO), bids=[(Decimal("0.50"), Decimal("100"))], asks=[(Decimal("0.51"), Decimal("100"))])
    )
    coord.market_state = store
    return coord


def _seed_both(coord: RuntimeCoordinator, qty: Decimal = Decimal("5")) -> None:
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(YES), qty, correlation_id="seed-y")
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(NO), qty, correlation_id="seed-n")
    coord.wallet.positions[TokenId(YES)] = WalletPosition(
        token_id=TokenId(YES), qty=qty, avg_price_usd=Decimal("0.49")
    )
    coord.wallet.positions[TokenId(NO)] = WalletPosition(
        token_id=TokenId(NO), qty=qty, avg_price_usd=Decimal("0.51")
    )


def _active_monitor_state(cfg: PairedBinaryStrategyConfig, **over) -> PairedBinaryRuntimeState:
    base = dict(
        phase=PairedBinaryPhase.BOTH_LEGS_ACTIVE,
        pair_correlation_id="pc1",
        yes_entry=Decimal("0.49"),
        no_entry=Decimal("0.51"),
        yes_activation_bid=Decimal("0.48"),
        no_activation_bid=Decimal("0.50"),
        activation_ts=monotonic_s() - 10,
        effective_qty=Decimal("5"),
        pair_opened_ts=monotonic_s(),
    )
    base.update(over)
    state = PairedBinaryRuntimeState(**base)
    ensure_pnl_budgets(state, cfg)
    return state


def test_yes_stop_loss_emits_urgent_sell_yes(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    _seed_both(coord)
    cfg = _cfg()
    state = _active_monitor_state(cfg)
    store = coord.market_state
    store.apply_snapshot(
        make_snapshot(TokenId(YES), bids=[(Decimal("0.43"), Decimal("100"))], asks=[(Decimal("0.44"), Decimal("100"))])
    )
    monitor = PairedBinaryMonitor(cfg)
    work = monitor.tick(coord, state)
    assert len(work) == 1
    assert isinstance(work[0].intent, ExitIntent)
    assert work[0].intent.side == Side.SELL
    assert str(work[0].intent.token_id) == YES
    assert state.phase == PairedBinaryPhase.STOP_PENDING_YES
    confirm_exit_submitted(state, "yes")
    assert state.phase == PairedBinaryPhase.EXITING_YES


def test_no_stop_loss_emits_urgent_sell_no(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    _seed_both(coord)
    cfg = _cfg()
    state = _active_monitor_state(cfg)
    store = coord.market_state
    store.apply_snapshot(
        make_snapshot(TokenId(NO), bids=[(Decimal("0.45"), Decimal("100"))], asks=[(Decimal("0.46"), Decimal("100"))])
    )
    monitor = PairedBinaryMonitor(cfg)
    work = monitor.tick(coord, state)
    assert len(work) == 1
    assert str(work[0].intent.token_id) == NO
    assert state.phase == PairedBinaryPhase.STOP_PENDING_NO


def test_only_yes_take_profit_emits_sell_yes(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(YES), Decimal("5"), correlation_id="y")
    coord.wallet.positions[TokenId(YES)] = WalletPosition(
        token_id=TokenId(YES), qty=Decimal("5"), avg_price_usd=Decimal("0.49")
    )
    cfg = _cfg()
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.ONLY_YES_ACTIVE,
        pair_correlation_id="pc1",
        yes_entry=Decimal("0.49"),
        yes_target=Decimal("0.55"),
        effective_qty=Decimal("5"),
        pair_opened_ts=monotonic_s(),
    )
    store = coord.market_state
    store.apply_snapshot(
        make_snapshot(TokenId(YES), bids=[(Decimal("0.56"), Decimal("100"))], asks=[(Decimal("0.57"), Decimal("100"))])
    )
    monitor = PairedBinaryMonitor(cfg)
    work = monitor.tick(coord, state)
    assert len(work) == 1
    assert str(work[0].intent.token_id) == YES


def test_only_no_take_profit_emits_sell_no(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(NO), Decimal("5"), correlation_id="n")
    coord.wallet.positions[TokenId(NO)] = WalletPosition(
        token_id=TokenId(NO), qty=Decimal("5"), avg_price_usd=Decimal("0.51")
    )
    cfg = _cfg()
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.ONLY_NO_ACTIVE,
        pair_correlation_id="pc1",
        no_entry=Decimal("0.51"),
        no_target=Decimal("0.60"),
        effective_qty=Decimal("5"),
        pair_opened_ts=monotonic_s(),
    )
    store = coord.market_state
    store.apply_snapshot(
        make_snapshot(TokenId(NO), bids=[(Decimal("0.61"), Decimal("100"))], asks=[(Decimal("0.62"), Decimal("100"))])
    )
    monitor = PairedBinaryMonitor(cfg)
    work = monitor.tick(coord, state)
    assert len(work) == 1
    assert str(work[0].intent.token_id) == NO


def test_max_holding_timeout_exits_both_legs(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    _seed_both(coord)
    cfg = _cfg(max_holding_time_s=0.001)
    state = _active_monitor_state(cfg, pair_opened_ts=0.0)
    monitor = PairedBinaryMonitor(cfg)
    work = monitor.tick(coord, state)
    assert len(work) == 2
    tokens = {str(w.intent.token_id) for w in work}
    assert tokens == {YES, NO}


def test_monitor_does_not_call_oms(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    _seed_both(coord)
    cfg = _cfg(max_holding_time_s=0.001)
    state = _active_monitor_state(cfg, pair_opened_ts=0.0)
    monitor = PairedBinaryMonitor(cfg)
    work = monitor.tick(coord, state)
    assert work
    assert not hasattr(monitor, "oms")
