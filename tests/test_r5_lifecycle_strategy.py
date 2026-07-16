"""R5 lifecycle-aware strategy transitions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import CorrelationId, MarketId, RunId, TokenId, new_correlation_id
from tyrex_pm.core.intents import IntentKind
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.domain.polymarket.market import BinaryMarket, MarketStatus, make_binary_instruments
from tyrex_pm.lifecycle.trade_lifecycle import LifecycleSnapshot, LifecycleState
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import ExecutableQuote
from tyrex_pm.market_data.freshness import (
    FreshnessAssessment,
    FreshnessConfig,
    FreshnessReason,
    TimestampBasis,
)
from tyrex_pm.runtime.config import ObserveConfig, RiskPlanConfig, SourceMode
from tyrex_pm.runtime.shadow_config import ShadowConfig
from tyrex_pm.runtime.shadow_host import ShadowHost
from tyrex_pm.signals.directional import Direction, DirectionalSignal
from tyrex_pm.strategies.context import DecisionContext
from tyrex_pm.strategies.framework_validation.reference_momentum import (
    ReferenceMomentumStrategy,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "r3_observe_complete.json"
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
        momentum=Decimal("0.01") if direction is Direction.UP else Decimal("-0.01"),
        threshold=Decimal("0.001"),
        strength=Decimal("1"),
        selected_outcome=None,
        reason_code="TEST",
        evidence={},
    )


def _life(
    state: LifecycleState,
    *,
    instrument=None,
    activated_at=None,
) -> LifecycleSnapshot:
    return LifecycleSnapshot(
        state=state,
        instrument_id=instrument,
        entry_order_id=None,
        exit_order_id=None,
        activated_at=activated_at,
        updated_at=TS,
    )


def test_flat_entry_active_blocks_repeat() -> None:
    strat = ReferenceMomentumStrategy()
    snap = _snap()
    ctx = DecisionContext(
        run_id=RunId("r"),
        mode=RuntimeMode.SHADOW,
        snapshot=snap,
        target_notional=Decimal("5"),
        lifecycle=_life(LifecycleState.FLAT),
        now=TS,
    )
    r = strat.apply_transition(_sig(Direction.UP), ctx)
    assert len(r.intents) == 1
    assert r.intents[0].kind is IntentKind.ENTER

    ctx2 = DecisionContext(
        run_id=RunId("r"),
        mode=RuntimeMode.SHADOW,
        snapshot=snap,
        target_notional=Decimal("5"),
        lifecycle=_life(
            LifecycleState.ACTIVE,
            instrument=snap.market.yes.instrument_id,
            activated_at=TS,
        ),
        now=TS,
        max_hold=timedelta(hours=1),
        flatten_before_close=timedelta(0),
    )
    r2 = strat.apply_transition(_sig(Direction.UP), ctx2)
    assert r2.suppressed
    assert r2.suppress_reason == "ACTIVE_NO_EXIT"


def test_reversal_creates_exit() -> None:
    strat = ReferenceMomentumStrategy()
    snap = _snap()
    ctx = DecisionContext(
        run_id=RunId("r"),
        mode=RuntimeMode.SHADOW,
        snapshot=snap,
        target_notional=Decimal("5"),
        lifecycle=_life(
            LifecycleState.ACTIVE,
            instrument=snap.market.yes.instrument_id,
            activated_at=TS,
        ),
        now=TS,
        max_hold=timedelta(hours=1),
        flatten_before_close=timedelta(0),
    )
    r = strat.apply_transition(_sig(Direction.DOWN), ctx)
    assert len(r.intents) == 1
    assert r.intents[0].kind is IntentKind.EXIT
    assert r.intents[0].reason_code == "SIGNAL_REVERSAL"


def test_kill_switch_flatten_precedence() -> None:
    strat = ReferenceMomentumStrategy()
    snap = _snap()
    ctx = DecisionContext(
        run_id=RunId("r"),
        mode=RuntimeMode.SHADOW,
        snapshot=snap,
        target_notional=Decimal("5"),
        lifecycle=_life(
            LifecycleState.ACTIVE,
            instrument=snap.market.yes.instrument_id,
            activated_at=TS,
        ),
        now=TS,
        max_hold=timedelta(hours=1),
        flatten_before_close=timedelta(0),
        kill_switch_active=True,
    )
    r = strat.apply_transition(_sig(Direction.UP), ctx)
    assert len(r.intents) == 1
    assert r.intents[0].kind is IntentKind.FLATTEN
    assert r.intents[0].reason_code == "KILL_SWITCH"


def test_market_close_boundary_before_reversal() -> None:
    strat = ReferenceMomentumStrategy()
    snap = _snap()
    ctx = DecisionContext(
        run_id=RunId("r"),
        mode=RuntimeMode.SHADOW,
        snapshot=snap,
        target_notional=Decimal("5"),
        lifecycle=_life(
            LifecycleState.ACTIVE,
            instrument=snap.market.yes.instrument_id,
            activated_at=TS,
        ),
        now=snap.market.event_end - timedelta(seconds=10),
        max_hold=timedelta(hours=1),
        flatten_before_close=timedelta(seconds=30),
        kill_switch_active=False,
    )
    r = strat.apply_transition(_sig(Direction.DOWN), ctx)
    assert len(r.intents) == 1
    assert r.intents[0].kind is IntentKind.FLATTEN
    assert r.intents[0].reason_code == "MARKET_CLOSE_BOUNDARY"


def test_fixture_shadow_entry_fill(tmp_path: Path) -> None:
    cfg = ObserveConfig(
        mode=SourceMode.FIXTURE,
        output_path=tmp_path / "facts.jsonl",
        binance_symbol="BTCUSDT",
        momentum_lookback=timedelta(seconds=5),
        momentum_threshold=Decimal("0.001"),
        max_book_spread=Decimal("0.10"),
        freshness=FreshnessConfig(
            book_threshold_ms=60_000,
            reference_threshold_ms=60_000,
            future_tolerance_ms=500,
            timestamp_basis=TimestampBasis.EVENT_TIME,
        ),
        runtime_duration=None,
        fixture_path=FIXTURE,
        risk=RiskPlanConfig(
            runtime_mode=RuntimeMode.SHADOW,
            target_notional=Decimal("5"),
            max_notional=Decimal("10"),
            min_price=Decimal("0.01"),
            max_price=Decimal("0.99"),
            max_spread=Decimal("0.20"),
            min_liquidity_notional=Decimal("1"),
            no_entry_before_close=timedelta(0),
            duplicate_lifetime=timedelta(hours=1),
        ),
        shadow=ShadowConfig(
            enable_oms=True,
            max_position_notional=Decimal("20"),
            max_total_exposure=Decimal("50"),
            max_hold=timedelta(minutes=10),
            flatten_before_close=timedelta(seconds=30),
            exit_on_flat=True,
            persistence_path=tmp_path / "state.json",
            cancel_unfilled_residual=False,
        ),
    )
    clock = FakeClock(_wall=TS)
    host = ShadowHost(
        cfg,
        clock=clock,
        run_id=RunId("run-r5"),
        correlation_id=CorrelationId("corr-r5"),
    )
    try:
        result = host.run_fixture()
    finally:
        host.close()

    assert any(s.direction is Direction.UP for s in result.signals)
    assert len(host.commands) >= 1
    yes = host.registry.require_market().yes.instrument_id
    assert host.portfolio.net_quantity(yes) > 0
    assert host.lifecycle.state is LifecycleState.ACTIVE
