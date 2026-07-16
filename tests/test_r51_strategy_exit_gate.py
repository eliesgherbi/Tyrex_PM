"""R5.1 strategy: one exit request; suppress storms; escalate kill/close."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tyrex_pm.core.ids import MarketId, RunId, TokenId, new_correlation_id
from tyrex_pm.core.intents import IntentKind
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.domain.polymarket.market import BinaryMarket, MarketStatus, make_binary_instruments
from tyrex_pm.lifecycle.trade_lifecycle import LifecycleSnapshot, LifecycleState
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import ExecutableQuote
from tyrex_pm.market_data.freshness import FreshnessAssessment, FreshnessReason, TimestampBasis
from tyrex_pm.signals.directional import Direction, DirectionalSignal
from tyrex_pm.strategies.context import DecisionContext
from tyrex_pm.strategies.framework_validation.reference_momentum import (
    ReferenceMomentumStrategy,
)

TS = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def _fresh() -> FreshnessAssessment:
    return FreshnessAssessment(
        is_fresh=True,
        age_ms=1,
        threshold_ms=1000,
        timestamp_basis=TimestampBasis.EVENT_TIME,
        reason_code=FreshnessReason.FRESH,
        observed_at=TS,
    )


def _snap() -> DecisionSnapshot:
    mid = MarketId("m1")
    yes, no = make_binary_instruments(market_id=mid, yes_token=TokenId("y"), no_token=TokenId("n"))
    q = ExecutableQuote(
        best_bid=Decimal("0.48"),
        best_ask=Decimal("0.52"),
        mid=Decimal("0.5"),
        spread=Decimal("0.04"),
        bid_size_at_touch=Decimal("100"),
        ask_size_at_touch=Decimal("100"),
    )
    return DecisionSnapshot(
        market=BinaryMarket(
            market_id=mid,
            condition_id="m1",
            question="q",
            yes=yes,
            no=no,
            status=MarketStatus.ACTIVE,
            event_end=TS + timedelta(minutes=5),
        ),
        yes_book=None,
        no_book=None,
        yes_quote=q,
        no_quote=q,
        reference=None,
        yes_freshness=_fresh(),
        no_freshness=_fresh(),
        reference_freshness=_fresh(),
        observed_at=TS,
        correlation_id=new_correlation_id(),
    )


def _sig(direction: Direction) -> DirectionalSignal:
    return DirectionalSignal(
        direction=direction,
        observed_at=TS,
        correlation_id=new_correlation_id(),
        causation_id=None,
        momentum=Decimal("0.01"),
        threshold=Decimal("0.001"),
        strength=Decimal("1"),
        selected_outcome=None,
        reason_code="TEST",
        evidence={},
    )


def _life(state: LifecycleState, snap: DecisionSnapshot) -> LifecycleSnapshot:
    return LifecycleSnapshot(
        state=state,
        instrument_id=snap.market.yes.instrument_id,
        entry_order_id=None,
        exit_order_id=None,
        activated_at=TS,
        updated_at=TS,
    )


def test_active_emits_one_exit_then_outstanding_suppresses() -> None:
    strat = ReferenceMomentumStrategy()
    snap = _snap()
    ctx = DecisionContext(
        run_id=RunId("r"),
        mode=RuntimeMode.SHADOW,
        snapshot=snap,
        target_notional=Decimal("5"),
        lifecycle=_life(LifecycleState.ACTIVE, snap),
        now=TS,
        max_hold=timedelta(hours=1),
        flatten_before_close=timedelta(0),
        exit_allowed=True,
    )
    r1 = strat.apply_transition(_sig(Direction.DOWN), ctx)
    assert len(r1.intents) == 1
    assert r1.intents[0].kind is IntentKind.EXIT

    ctx2 = DecisionContext(
        run_id=RunId("r"),
        mode=RuntimeMode.SHADOW,
        snapshot=snap,
        target_notional=Decimal("5"),
        lifecycle=_life(LifecycleState.EXIT_REQUESTED, snap),
        now=TS,
        max_hold=timedelta(hours=1),
        flatten_before_close=timedelta(0),
        exit_allowed=False,
        exit_block_reason="EXIT_REQUESTED",
    )
    r2 = strat.apply_transition(_sig(Direction.DOWN), ctx2)
    assert r2.suppressed
    assert r2.suppress_reason == "EXIT_ALREADY_OUTSTANDING"
    assert r2.intents == []


def test_kill_switch_escalates_outstanding_exit() -> None:
    strat = ReferenceMomentumStrategy()
    snap = _snap()
    ctx = DecisionContext(
        run_id=RunId("r"),
        mode=RuntimeMode.SHADOW,
        snapshot=snap,
        target_notional=Decimal("5"),
        lifecycle=_life(LifecycleState.EXIT_RETRY_WAIT, snap),
        now=TS,
        max_hold=timedelta(hours=1),
        flatten_before_close=timedelta(0),
        kill_switch_active=True,
        exit_allowed=True,
        exit_escalate=True,
        exit_urgency="URGENT",
    )
    r = strat.apply_transition(_sig(Direction.FLAT), ctx)
    assert len(r.intents) == 1
    assert r.intents[0].kind is IntentKind.FLATTEN
    assert r.intents[0].reason_code == "KILL_SWITCH"


def test_entry_retry_wait_suppresses_storm() -> None:
    strat = ReferenceMomentumStrategy()
    snap = _snap()
    ctx = DecisionContext(
        run_id=RunId("r"),
        mode=RuntimeMode.SHADOW,
        snapshot=snap,
        target_notional=Decimal("5"),
        lifecycle=LifecycleSnapshot(
            state=LifecycleState.FLAT,
            instrument_id=None,
            entry_order_id=None,
            exit_order_id=None,
            activated_at=None,
            updated_at=TS,
        ),
        now=TS,
        entry_allowed=False,
        entry_block_reason="ENTRY_COOLDOWN",
    )
    r = strat.apply_transition(_sig(Direction.UP), ctx)
    assert r.suppressed
    assert r.suppress_reason == "ENTRY_COOLDOWN"
    assert r.intents == []


def test_residual_not_terminal_from_active() -> None:
    from tyrex_pm.lifecycle.trade_lifecycle import LifecycleError, TradeLifecycle
    from tyrex_pm.execution.order_store import OrderStore
    from tyrex_pm.execution.fill_ledger import FillLedger
    from tyrex_pm.portfolio.portfolio import Portfolio
    from helpers_r5 import MARKET

    life = TradeLifecycle(
        order_store=OrderStore(),
        portfolio=Portfolio(fill_ledger=FillLedger(), market_id=MARKET),
    )
    # Force ACTIVE without fills for terminal guard test
    life._state = LifecycleState.ACTIVE
    try:
        life.mark_terminal(when=TS)
        raised = False
    except LifecycleError:
        raised = True
    assert raised
