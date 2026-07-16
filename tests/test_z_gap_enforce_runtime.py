"""Enforce runtime integration tests (A0.7) — shadow/mocked only."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.market_data.book_read import LegBookQuote, PairBookSnapshot
from tyrex_pm.quant.binary_fair_value import FairValueSnapshot, MODEL_STATUS_READY
from tyrex_pm.quant.edge import EdgeSnapshot, LEG_UP
from tyrex_pm.quant.volatility import VolatilitySnapshot
from tyrex_pm.runtime.config import Z_GAP_ENTRY_MODE_ENFORCE, Z_GAP_ENTRY_MODE_OBSERVE_ONLY, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.time_authority import TimeAuthority
from tyrex_pm.runtime.z_gap_enforce import ZGapEnforceRuntimeState, run_enforce_tick
from tyrex_pm.runtime.z_gap_run import ObserveTickContext
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.signal_state_store import FRESHNESS_FRESH, FRESHNESS_OBSERVED, SignalSnapshot
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.z_gap.entry_eval import DECISION_WOULD_ENTER, ZGapEntryEvaluation
from tyrex_pm.strategies.z_gap.exit_eval import ZGapExitEvalState, evaluate_exit_triggers
from tyrex_pm.strategies.z_gap.facts import ZGapObserveRuntimeState
from tyrex_pm.strategies.z_gap.state import ZGapLifecycleState, ZGapPhase
from tyrex_pm.strategies.z_gap.strategy import ZGapStrategy
from tyrex_pm.runtime.z_gap_preflight import ZGapPreflightGates
from tyrex_pm.runtime.config import ZGapExitConfig

TS = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
EXIT = ZGapExitConfig(
    z_stop=Decimal("0.25"),
    stop_confirm_s=0.0,
    flatten_before_event_end_s=20,
    retry_interval_ms=0,
    max_exit_attempts=5,
)


def _app(entry_mode: str):
    return parse_app_config(
        risk={
            "notional": {"min_usd": "0.01", "max_usd": "5", "max_policy": "deny"},
            "deployment": {"token_cap_usd": "25", "portfolio_cap_usd": "100"},
            "venue_min_size": {"enabled": False},
            "capital": {"enabled": False, "max_wallet_age_s": 120},
            "concurrency": {"max_orders_in_flight": 4},
            "readiness": {
                "require_wallet_sync": False,
                "max_wallet_age_s_live": 120,
                "require_heartbeat_live": False,
                "require_user_ws_live": False,
            },
        },
        strategy={
            "kind": "z_gap",
            "enabled": True,
            "z_gap": {
                "entry_mode": entry_mode,
                "market_id": "btc_5m_test",
                "condition_id": "0xabc",
                "yes_token_id": "111",
                "no_token_id": "222",
                "event_start_ts": time.time() - 60,
                "event_end_ts": time.time() + 3600,
                "sigma": {"min_samples_s": 1, "sample_interval_s": 0},
                "entry": {
                    "theta_take": "0.05",
                    "theta_fill_floor": "0.03",
                    "z_band": ["0.8", "2.2"],
                    "tau_band_s": [60, 210],
                    "basis_max_bps": "3",
                    "expected_slippage_ticks": 1,
                },
                "sizing": {"mode": "fixed_usd", "max_usd": "5", "min_shares": "5"},
                "exit": {
                    "z_stop": "0.25",
                    "stop_confirm_s": 0,
                    "flatten_before_event_end_s": 20,
                    "retry_interval_ms": 0,
                    "max_exit_attempts": 5,
                },
            },
        },
        runtime={
            "execution_mode": "shadow",
            "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
            "market_data": {"enabled": True, "max_book_age_s": 5},
            "signal_feeds": {"enabled": False},
        },
    )


def test_observe_mode_unchanged_guard() -> None:
    app = _app(Z_GAP_ENTRY_MODE_OBSERVE_ONLY)
    app = replace(app, z_gap=replace(app.z_gap, entry_mode=Z_GAP_ENTRY_MODE_ENFORCE))
    with pytest.raises(RuntimeError, match="observe_only"):
        from tyrex_pm.runtime.z_gap_run import run_observe_tick

        run_observe_tick(
            ObserveTickContext(
                app=app,
                zg=app.z_gap,
                run_id=RunId("r1"),
                coord=RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime()),
                sink=MagicMock(),
                state=ZGapObserveRuntimeState(),
                estimator=MagicMock(),
            ),
            allow_enforce=False,
        )


def _vol_enforce(**over):
    base = dict(
        sigma=0.00015,
        sigma_units="per_sqrt_second",
        ready=True,
        sample_count=10,
        effective_samples_s=10.0,
        last_update_ts=TS,
        jump_guard_tripped=False,
        reject_reason=None,
    )
    base.update(over)
    return VolatilitySnapshot(**base)


def test_kill_switch_precedence_over_thesis() -> None:
    lc = ZGapLifecycleState(market_id="m", condition_id="0x", owner_id="z_gap")
    lc.phase = ZGapPhase.ACTIVE
    lc.selected_leg = LEG_UP
    fair = FairValueSnapshot(
        S=Decimal("100"),
        K=Decimal("100"),
        tau_s=120.0,
        sigma=0.02,
        sigma_units="per_sqrt_s",
        z=-1.0,
        p_up=0.6,
        p_down=0.4,
        model_status=MODEL_STATUS_READY,
        reject_reason=None,
        snapshot_ts=TS,
    )
    vol = _vol_enforce()
    ev = ZGapExitEvalState()
    d = evaluate_exit_triggers(
        lifecycle=lc,
        fair=fair,
        vol=vol,
        exit_cfg=EXIT,
        eval_state=ev,
        event_end_ts=time.time() + 1000,
        time_authority=None,
        now_ts=time.time(),
        feeds_fresh=True,
        manual_intervention=True,
    )
    assert d.should_exit
    assert d.exit_reason == "kill_switch"


def test_lifecycle_flatten_produces_exit_reason() -> None:
    lc = ZGapLifecycleState(market_id="m", condition_id="0x", owner_id="z_gap")
    lc.phase = ZGapPhase.ACTIVE
    lc.selected_leg = LEG_UP
    fair = FairValueSnapshot(
        S=Decimal("100"),
        K=Decimal("100"),
        tau_s=10.0,
        sigma=0.02,
        sigma_units="per_sqrt_s",
        z=1.0,
        p_up=0.6,
        p_down=0.4,
        model_status=MODEL_STATUS_READY,
        reject_reason=None,
        snapshot_ts=TS,
    )
    vol = _vol_enforce()
    ev = ZGapExitEvalState()
    end = time.time() + 10
    d = evaluate_exit_triggers(
        lifecycle=lc,
        fair=fair,
        vol=vol,
        exit_cfg=EXIT,
        eval_state=ev,
        event_end_ts=end,
        time_authority=TimeAuthority(sync_status="synced", samples_requested=1, samples_kept=1),
        now_ts=end - 5,
        feeds_fresh=True,
    )
    assert d.should_exit
    assert d.exit_reason == "lifecycle_flatten"


@pytest.mark.asyncio
async def test_enforce_tick_does_not_submit_without_preflight() -> None:
    with patch("tyrex_pm.runtime.z_gap_live.validate_z_gap_live_config"):
        app = _app(Z_GAP_ENTRY_MODE_ENFORCE)
    coord = RuntimeCoordinator(
        wallet=WalletStore(),
        orders=OrderStore(),
        health=HealthRuntime(),
        allocation_ledger=AllocationLedger(),
    )
    state = ZGapObserveRuntimeState()
    lc = ZGapLifecycleState(market_id=app.z_gap.market_id, condition_id="0xabc", owner_id="z_gap")
    enforce = ZGapEnforceRuntimeState(observe=state, lifecycle=lc)
    strategy = ZGapStrategy(app.z_gap)
    sink = MagicMock()

    evaln = MagicMock(decision_status=DECISION_WOULD_ENTER)
    state.last_edge = MagicMock()
    state.last_fair = MagicMock()
    state.fee_model_status = "resolved"

    with patch("tyrex_pm.runtime.z_gap_enforce.run_observe_tick", return_value=evaln), patch(
        "tyrex_pm.runtime.z_gap_enforce.read_pair_books",
        return_value=PairBookSnapshot(
            up=LegBookQuote("111", Decimal("0.59"), Decimal("0.6"), False, 1, Decimal("0.01"), "ok"),
            down=LegBookQuote("222", Decimal("0.39"), Decimal("0.4"), False, 1, Decimal("0.01"), "ok"),
        ),
    ), patch("tyrex_pm.runtime.z_gap_enforce.load_z_gap_preflight_gates") as pf:
        pf.return_value = ZGapPreflightGates(
            blockers=("operator_approved_enforce: missing",),
            operator_approved_enforce=False,
        )
        ctx = ObserveTickContext(
            app=app,
            zg=app.z_gap,
            run_id=RunId("r-enf"),
            coord=coord,
            sink=sink,
            state=state,
            estimator=MagicMock(snapshot=lambda: _vol_enforce()),
            entry_cfg=app.z_gap.entry,
            lifecycle=lc,
        )
        await run_enforce_tick(
            ctx=ctx,
            enforce_state=enforce,
            strategy=strategy,
            oms=ShadowOMS(),
            exit_cfg=EXIT,
            apply_local_shadow_fill=True,
        )
    assert lc.phase == ZGapPhase.IDLE
    assert not lc.entry_submitted
