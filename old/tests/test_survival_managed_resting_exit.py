"""Managed resting survival exit lifecycle tests."""

from __future__ import annotations

import time
from decimal import Decimal

from tyrex_pm.core.models import WalletPosition
from tyrex_pm.core.ids import TokenId
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.market_data_runtime import inject_fixture_book
from tyrex_pm.state.market_store import MarketStateStore
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.monitor import PairedBinaryMonitor
from tyrex_pm.strategies.paired_binary.state import LegRuntime, PairedBinaryPhase, PairedBinaryRuntimeState
from tyrex_pm.survival.enforcement_dispatch import (
    mark_resting_order_placed,
    resting_order_open,
    resting_ttl_expired,
)
from tyrex_pm.survival.order_policy import SurvivalRestingState

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"


def _state_with_resting(*, placed_ts: float | None = None, ttl: float = 2.0) -> PairedBinaryRuntimeState:
    ts = placed_ts if placed_ts is not None else time.time() - 3.0
    return PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.TP_PENDING_YES,
        effective_qty=Decimal("5"),
        yes=LegRuntime(triggered=False, entry_qty=Decimal("5")),
        no=LegRuntime(triggered=True, exit_qty=Decimal("5")),
        survivor_leg_state={
            "survival_resting_order_id": "ord-123",
            "survival_resting_state": SurvivalRestingState.RESTING_PLACED.value,
            "survival_resting_placed_ts": ts,
            "survival_resting_local_ttl_s": ttl,
            "enforce_module": "trailing_stop",
        },
    )


def test_resting_ttl_expired() -> None:
    state = _state_with_resting()
    assert resting_ttl_expired(state) is True
    assert resting_order_open(state) is True


def test_monitor_emits_cancel_on_ttl_expired() -> None:
    from tyrex_pm.runtime.config import parse_app_config

    app = parse_app_config(
        risk={"notional": {"min_usd": "1", "max_usd": "100", "max_policy": "cap"}},
        runtime={
            "market_data": {"enabled": True},
            "execution": {"planner": {"enabled": True}},
        },
        strategy={
            "kind": "paired_binary",
            "paired_binary": {
                "owner_id": "pb",
                "market_id": "m1",
                "yes_token_id": YES,
                "no_token_id": NO,
                "position_size": "5",
                "use_fixture_book": True,
                "exit_order_style": "FAK",
                "max_pair_entry_cost": "1.02",
                "max_spread_yes": "0.05",
                "max_spread_no": "0.05",
            },
        },
    )
    cfg = app.paired_binary
    assert cfg is not None
    monitor = PairedBinaryMonitor(cfg)
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    inject_fixture_book(coord, YES, best_bid=Decimal("0.60"), best_ask=Decimal("0.61"))
    inject_fixture_book(coord, NO, best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))
    state = _state_with_resting()
    work = monitor._maybe_manage_survival_resting_orders(
        coord, state, monitor._books(coord)[0], monitor._books(coord)[1], None, None, app=app
    )
    assert len(work) == 1
    ext = work[0].intent_fact_extensions or {}
    assert ext.get("survival_resting_cancel") is True
    assert state.survivor_leg_state["survival_resting_state"] == SurvivalRestingState.RESTING_CANCEL_REQUESTED.value


def test_mark_resting_placed_clears_in_flight() -> None:
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.TP_PENDING_YES,
        survivor_leg_state={"enforce_exit_in_flight": True},
    )
    mark_resting_order_placed(state, order_id="x", local_ttl_s=2.0)
    assert state.survivor_leg_state["enforce_exit_in_flight"] is False
    assert resting_order_open(state) is True
