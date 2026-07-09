"""Shared helpers for paired-binary shutdown policy tests."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import WalletPosition
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import (
    OPEN_EXPOSURE_ON_MAX_RUNTIME_FORCE,
    ShadowBootstrapConfig,
    parse_app_config,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.market_data_runtime import inject_fixture_book
from tyrex_pm.runtime.paired_binary_recovery import persistence_path, recover_on_startup
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.exit_engine import ensure_pnl_budgets
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState, save_persisted_state

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"


def risk_cfg(**over):
    base = {
        "notional": {"min_usd": "1", "max_usd": "100", "max_policy": "cap"},
        "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
        "venue_min_size": {"enabled": False},
        "capital": {"enabled": False},
        "inventory": {"sell_requires_venue_position": False},
        "concurrency": {"max_orders_in_flight": 10},
        "readiness": {"require_wallet_sync": False},
    }
    base.update(over)
    return base


def strategy_cfg(**pb_over):
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
        "use_fixture_book": True,
        "fixture_yes_bid": "0.48",
        "fixture_yes_ask": "0.49",
        "fixture_no_bid": "0.50",
        "fixture_no_ask": "0.51",
        "tick_interval_s": 0.005,
        "max_runtime_s": 0.01,
        "exit_order_style": "FAK",
    }
    pb.update(pb_over)
    return {"kind": "paired_binary", "enabled": True, "paired_binary": pb}


def app_cfg(*, open_exposure_policy=OPEN_EXPOSURE_ON_MAX_RUNTIME_FORCE, risk=None, allow_terminal_state_resume=False, **pb_over):
    return parse_app_config(
        risk=risk or risk_cfg(),
        strategy=strategy_cfg(**pb_over),
        runtime={
            "execution_mode": "shadow",
            "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
            "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
            "market_data": {"enabled": True, "max_book_age_s": 5},
            "execution": {"planner": {"enabled": True}},
            "observability": {"emit_decision_snapshot": True},
            "paired_binary": {
                "poll_interval_s": 0.005,
                "open_exposure_on_max_runtime": open_exposure_policy,
                "open_exposure_timeout_s": 0.05,
                "allow_terminal_state_resume": allow_terminal_state_resume,
            },
        },
    )


def coord_with_books(tmp_path: Path) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    apply_shadow_bootstrap(
        coord.wallet,
        ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")),
    )
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    inject_fixture_book(coord, YES, best_bid=Decimal("0.48"), best_ask=Decimal("0.49"))
    inject_fixture_book(coord, NO, best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))
    return coord


def seed_both_legs(coord: RuntimeCoordinator, *, qty: Decimal = Decimal("5")) -> None:
    coord.wallet.positions[TokenId(YES)] = WalletPosition(
        token_id=TokenId(YES), qty=qty, avg_price_usd=Decimal("0.50")
    )
    coord.wallet.positions[TokenId(NO)] = WalletPosition(
        token_id=TokenId(NO), qty=qty, avg_price_usd=Decimal("0.51")
    )
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(YES), qty, correlation_id="seed-y")
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(NO), qty, correlation_id="seed-n")


def seed_no_leg(coord: RuntimeCoordinator, *, qty: Decimal = Decimal("5")) -> None:
    coord.wallet.positions[TokenId(NO)] = WalletPosition(
        token_id=TokenId(NO), qty=qty, avg_price_usd=Decimal("0.47")
    )
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(NO), qty, correlation_id="seed-n")


def both_legs_active_state() -> PairedBinaryRuntimeState:
    st = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.BOTH_LEGS_ACTIVE,
        pair_correlation_id="pc_live4",
        owner_id="paired_binary",
        market_id="m1",
        yes_token_id=YES,
        no_token_id=NO,
        yes_entry=Decimal("0.50"),
        no_entry=Decimal("0.51"),
        effective_qty=Decimal("5"),
        pair_cost=Decimal("1.01"),
        pair_opened_ts=monotonic_s(),
        activation_ts=monotonic_s(),
    )
    return st


def only_no_state() -> PairedBinaryRuntimeState:
    return PairedBinaryRuntimeState(
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


def facts_from_sink(sink: JsonlSink) -> list[dict]:
    path = sink._path
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def persist_file(tmp_path: Path, owner_id: str = "paired_binary", market_id: str = "m1") -> Path:
    return persistence_path(tmp_path, owner_id, market_id)


def write_persisted_state(
    tmp_path: Path,
    state: PairedBinaryRuntimeState,
    *,
    owner_id: str = "paired_binary",
    market_id: str = "m1",
) -> Path:
    path = persist_file(tmp_path, owner_id, market_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    save_persisted_state(path, state)
    return path
