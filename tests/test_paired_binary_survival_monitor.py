"""Monitor integration: survival advisory emits facts without OMS submit."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.models import WalletPosition
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import SurvivalConfig, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.market_data_runtime import inject_fixture_book
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.monitor import PairedBinaryMonitor
from tyrex_pm.strategies.paired_binary.state import LegRuntime, PairedBinaryPhase, PairedBinaryRuntimeState

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"


def _app_cfg(*, survival_enabled: bool):
    return parse_app_config(
        risk={
            "notional": {"min_usd": "1", "max_usd": "100", "max_policy": "cap"},
            "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
            "venue_min_size": {"enabled": False},
            "capital": {"enabled": False},
            "inventory": {"sell_requires_venue_position": False},
            "concurrency": {"max_orders_in_flight": 10},
            "readiness": {"require_wallet_sync": False},
        },
        strategy={
            "kind": "paired_binary",
            "paired_binary": {
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
                "tick_interval_s": 0.005,
                "max_runtime_s": 0.01,
                "exit_order_style": "FAK",
            },
        },
        runtime={
            "execution_mode": "shadow",
            "market_data": {"enabled": True, "max_book_age_s": 5},
            "execution": {"planner": {"enabled": True}},
            "survival": {"enabled": survival_enabled},
        },
    )


def _coord() -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    coord.allocation_ledger = AllocationLedger(path=Path("var/test-ledger-survival-monitor.json"))
    inject_fixture_book(coord, YES, best_bid=Decimal("0.55"), best_ask=Decimal("0.56"))
    inject_fixture_book(coord, NO, best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(YES), Decimal("5"), correlation_id="y")
    coord.wallet.positions[TokenId(YES)] = WalletPosition(
        token_id=TokenId(YES), qty=Decimal("5"), avg_price_usd=Decimal("0.48")
    )
    return coord


def _survivor_state(*, target: str = "0.70") -> PairedBinaryRuntimeState:
    return PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.ONLY_YES_ACTIVE,
        effective_qty=Decimal("5"),
        yes_entry=Decimal("0.48"),
        no_entry=Decimal("0.52"),
        yes_target=Decimal(target),
        yes_trigger_stop=Decimal("0.40"),
        no_trigger_stop=Decimal("0.40"),
        pair_cost=Decimal("1.00"),
        yes=LegRuntime(
            triggered=False,
            entry_cash=Decimal("2.40"),
            entry_qty=Decimal("5"),
            exit_cash=Decimal("2.00"),
            exit_qty=Decimal("5"),
        ),
        no=LegRuntime(
            triggered=True,
            entry_cash=Decimal("2.60"),
            entry_qty=Decimal("5"),
        ),
        survivor_leg_state={
            "survivor_bid_0": "0.50",
            "breakeven_price": "0.54",
            "hard_floor_price": "0.48",
            "loser_exit_ts": __import__("time").time() - 30,
        },
    )


def test_survival_advisory_emits_facts_no_oms(tmp_path: Path) -> None:
    import json

    app = _app_cfg(survival_enabled=True)
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord()
    state = _survivor_state()
    monitor = PairedBinaryMonitor(cfg)
    facts_path = tmp_path / "facts.jsonl"
    with JsonlSink(facts_path) as sink:
        work = monitor.tick(coord, state, sink=sink, run_id=RunId("surv-mon"), app=app)
        facts = [json.loads(line) for line in facts_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert work == []
    types = {f["fact_type"] for f in facts}
    assert "survival_monitor_evaluated" in types
    assert "survivor_stall_detected" not in types
    assert "survivor_reachability_scored" not in types


def test_survival_disabled_monitor_unchanged(tmp_path: Path) -> None:
    app = _app_cfg(survival_enabled=False)
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord()
    state = _survivor_state(target="0.54")
    monitor = PairedBinaryMonitor(cfg)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        work = monitor.tick(coord, state, sink=sink, run_id=RunId("surv-off"), app=app)
    assert len(work) == 1
    assert work[0].intent.side.value == "SELL"
