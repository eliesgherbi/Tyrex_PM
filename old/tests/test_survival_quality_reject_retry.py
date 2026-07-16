"""Survival enforce quality-reject retry lifecycle tests."""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import WalletPosition
from tyrex_pm.market_data.quality import DataQualityReport, QualityVerdict
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.market_data_runtime import inject_fixture_book
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.monitor import PairedBinaryMonitor
from tyrex_pm.strategies.paired_binary.state import LegRuntime, PairedBinaryPhase, PairedBinaryRuntimeState
from tyrex_pm.survival.enforcement_dispatch import (
    abandon_pending_survival_exit_intent,
    abandon_reason_for_pending_survival_exit,
    has_pending_survival_exit_intent,
    latch_pending_survival_exit_intent,
    should_retry_pending_survival_exit,
)
from tyrex_pm.survival.models import ExecutableExitEvidence, SurvivalAdvisoryResult, SurvivalExitEvaluation
from tyrex_pm.survival.quality_reject_detail import build_quality_reject_detail

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"


def _app(*, retry: bool = True):
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
            "execution": {"planner": {"enabled": True, "use_executable_depth": True}},
            "survival": {
                "enabled": True,
                "trailing_stop": {
                    "enabled": True,
                    "enforcement_mode": "enforce",
                    "activation_mode": "executable_gain",
                    "arm_delay_s": 0,
                    "arm_after_executable_gain": "0.01",
                    "trail_distance": "0.02",
                },
                "enforcement": {
                    "retry_quality_rejects": retry,
                    "max_quality_reject_retries": 10,
                    "quality_reject_retry_backoff_s": 0.25,
                    "abandon_quality_reject_after_s": 10,
                },
            },
        },
    )


def _coord() -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    coord.allocation_ledger = AllocationLedger(path=Path("var/test-ledger-quality-retry.json"))
    inject_fixture_book(coord, YES, best_bid=Decimal("0.57"), best_ask=Decimal("0.58"))
    inject_fixture_book(coord, NO, best_bid=Decimal("0.50"), best_ask=Decimal("0.51"))
    coord.allocation_ledger.apply_buy("paired_binary", TokenId(YES), Decimal("5"), correlation_id="y")
    coord.wallet.positions[TokenId(YES)] = WalletPosition(
        token_id=TokenId(YES), qty=Decimal("5"), avg_price_usd=Decimal("0.48")
    )
    return coord


def _survivor_state() -> PairedBinaryRuntimeState:
    return PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.ONLY_YES_ACTIVE,
        effective_qty=Decimal("5"),
        yes_entry=Decimal("0.48"),
        no_entry=Decimal("0.52"),
        yes_target=Decimal("0.70"),
        pair_cost=Decimal("1.00"),
        yes=LegRuntime(entry_cash=Decimal("2.40"), entry_qty=Decimal("5")),
        no=LegRuntime(triggered=True, exit_cash=Decimal("2.00"), exit_qty=Decimal("5")),
        survivor_leg_state={
            "survivor_bid_0": "0.50",
            "selected_target": "0.70",
            "selected_mode": "full_recovery",
            "loser_exit_ts": time.time() - 120,
            "trailing_stop": {
                "state": "triggered",
                "peak_executable_bid": "0.60",
                "trail_floor": "0.58",
            },
        },
    )


def _quality_reject_eval() -> SurvivalExitEvaluation:
    ev = ExecutableExitEvidence(
        touch_bid=Decimal("0.57"),
        executable_bid=Decimal("0.57"),
        sweep_vwap=Decimal("0.57"),
        worst_price_to_fill=Decimal("0.57"),
        available_depth=Decimal("100"),
        available_depth_fraction=Decimal("1"),
        expected_slippage=Decimal("0"),
        book_age_ms=500,
        snapshot_id="snap-1",
        quality_verdict="reject_decision",
        spread=Decimal("0.01"),
        planner_evidence_ref=None,
        quality_reject_detail={
            "failed_freshness": False,
            "failed_spread": False,
            "failed_depth": False,
            "failed_sequence_gap": True,
        },
    )
    return SurvivalExitEvaluation(
        verdict="defer",
        recommended_qty=Decimal("0"),
        evidence=ev,
        reason="quality_reject",
    )


def test_build_quality_reject_detail_sequence_gap() -> None:
    report = DataQualityReport(
        verdict=QualityVerdict.REJECT_DECISION,
        reasons=("reconnect_gap",),
        book_age_ms=500,
        source="websocket",
        source_quality="ws_primary",
        reconnect_gap=True,
        spread=Decimal("0.01"),
        depth_at_size=Decimal("100"),
        profile_id="crypto_5m",
    )
    detail = build_quality_reject_detail(report)
    assert detail["failed_sequence_gap"] is True
    assert detail["failed_spread"] is False


def test_latch_pending_survival_exit_intent() -> None:
    state = _survivor_state()
    assert not has_pending_survival_exit_intent(state)
    latch_pending_survival_exit_intent(
        state,
        module="trailing_stop",
        reason="trail_floor_breach",
        trigger_type="survival_trailing_stop",
        skip_reason="quality_reject",
        quality_reject_detail={"failed_sequence_gap": True},
    )
    assert has_pending_survival_exit_intent(state)
    raw = state.survivor_leg_state or {}
    assert raw["pending_survival_exit_module"] == "trailing_stop"
    assert raw["pending_survival_exit_trigger_type"] == "survival_trailing_stop"


def test_quality_reject_skip_latches_pending_intent(tmp_path: Path) -> None:
    app = _app(retry=True)
    monitor = PairedBinaryMonitor(app.paired_binary)
    state = _survivor_state()
    coord = _coord()
    result = SurvivalAdvisoryResult(
        exit_eval=_quality_reject_eval(),
        reachability=None,
        stall=None,
        trailing=None,
        economics=None,
        enforce_exit=True,
        enforce_exit_reason="trail_floor_breach",
        enforce_module="trailing_stop",
    )
    facts_path = tmp_path / "facts.jsonl"
    yes_book, no_book = _books(coord)
    with JsonlSink(facts_path) as sink:
        work = monitor._maybe_dispatch_survival_enforcement(
            app=app,
            coord=coord,
            state=state,
            yes_book=yes_book,
            no_book=no_book,
            sink=sink,
            run_id=RunId("latch"),
            pair_id="pair-1",
            result=result,
            flatten_before_event_end_s=20.0,
        )
    assert work == []
    assert has_pending_survival_exit_intent(state)
    rows = [json.loads(l) for l in facts_path.read_text().splitlines() if l.strip()]
    skipped = [r for r in rows if r["fact_type"] == "survival_enforce_exit_skipped"][0]
    assert skipped["payload"]["retryable"] is True
    assert skipped["payload"]["latched_intent"] is True
    assert "survival_enforce_exit_retry_scheduled" in {r["fact_type"] for r in rows}


def test_retry_submits_exit_when_quality_passes(tmp_path: Path) -> None:
    app = _app(retry=True)
    monitor = PairedBinaryMonitor(app.paired_binary)
    state = _survivor_state()
    coord = _coord()
    latch_pending_survival_exit_intent(
        state,
        module="trailing_stop",
        reason="trail_floor_breach",
        trigger_type="survival_trailing_stop",
        skip_reason="quality_reject",
        quality_reject_detail={"failed_sequence_gap": True},
    )
    raw = state.survivor_leg_state or {}
    raw["pending_survival_exit_last_attempt_ts"] = time.time() - 1.0

    facts_path = tmp_path / "facts.jsonl"
    with JsonlSink(facts_path) as sink:
        work = monitor._retry_pending_survival_quality_exit(
            coord, state, *_books(coord), sink, RunId("retry-pass"), app=app
        )
    assert len(work) == 1
    assert not has_pending_survival_exit_intent(state)
    rows = [json.loads(l) for l in facts_path.read_text().splitlines() if l.strip()]
    types = {r["fact_type"] for r in rows}
    assert "survival_enforce_exit_retry_attempted" in types
    assert "survival_enforce_exit_submitted" in types


def _books(coord: RuntimeCoordinator):
    from tyrex_pm.strategies.paired_binary.entry_eval import read_leg_book

    store = coord.market_state
    yes = read_leg_book(store, TokenId(YES), max_book_age_s=5.0)
    no = read_leg_book(store, TokenId(NO), max_book_age_s=5.0)
    return yes, no


def test_retry_respects_backoff(tmp_path: Path) -> None:
    app = _app(retry=True)
    monitor = PairedBinaryMonitor(app.paired_binary)
    state = _survivor_state()
    coord = _coord()
    latch_pending_survival_exit_intent(
        state,
        module="trailing_stop",
        reason="trail_floor_breach",
        trigger_type="survival_trailing_stop",
        skip_reason="quality_reject",
        quality_reject_detail={"failed_sequence_gap": True},
    )
    ok, reason = should_retry_pending_survival_exit(state, app.survival.enforcement)
    assert ok is False
    assert reason == "backoff"
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        work = monitor._retry_pending_survival_quality_exit(
            coord, state, *_books(coord), sink, RunId("backoff"), app=app
        )
    assert work == []


def test_retry_abandons_after_max_attempts() -> None:
    app = _app(retry=True)
    state = _survivor_state()
    latch_pending_survival_exit_intent(
        state,
        module="trailing_stop",
        reason="trail_floor_breach",
        trigger_type="survival_trailing_stop",
        skip_reason="quality_reject",
        quality_reject_detail={"failed_sequence_gap": True},
    )
    raw = state.survivor_leg_state or {}
    raw["pending_survival_exit_attempt_count"] = 10
    raw["pending_survival_exit_last_attempt_ts"] = time.time() - 1.0
    reason = abandon_reason_for_pending_survival_exit(state, app.survival.enforcement)
    assert reason == "max_retries"
    ctx = abandon_pending_survival_exit_intent(state, reason="max_retries")
    assert ctx["module"] == "trailing_stop"
    assert not has_pending_survival_exit_intent(state)


def test_pre_close_preempt_abandons_pending_intent() -> None:
    state = _survivor_state()
    latch_pending_survival_exit_intent(
        state,
        module="trailing_stop",
        reason="trail_floor_breach",
        trigger_type="survival_trailing_stop",
        skip_reason="quality_reject",
        quality_reject_detail={"failed_sequence_gap": True},
    )
    ctx = abandon_pending_survival_exit_intent(state, reason="pre_close_flatten_preempted")
    assert ctx["abandon_reason"] == "pre_close_flatten_preempted"
    assert not has_pending_survival_exit_intent(state)


def test_retry_disabled_does_not_latch(tmp_path: Path) -> None:
    app = _app(retry=False)
    monitor = PairedBinaryMonitor(app.paired_binary)
    state = _survivor_state()
    coord = _coord()
    result = SurvivalAdvisoryResult(
        exit_eval=_quality_reject_eval(),
        reachability=None,
        stall=None,
        trailing=None,
        economics=None,
        enforce_exit=True,
        enforce_exit_reason="trail_floor_breach",
        enforce_module="trailing_stop",
    )
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        monitor._maybe_dispatch_survival_enforcement(
            app=app,
            coord=coord,
            state=state,
            yes_book=_books(coord)[0],
            no_book=_books(coord)[1],
            sink=sink,
            run_id=RunId("no-retry"),
            pair_id="pair-1",
            result=result,
            flatten_before_event_end_s=20.0,
        )
    assert not has_pending_survival_exit_intent(state)
