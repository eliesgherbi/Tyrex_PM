"""R4 strategy transition policy tests."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.ids import CorrelationId, MarketId, RunId, TokenId, new_correlation_id
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.domain.polymarket.market import BinaryMarket, MarketStatus, make_binary_instruments
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import ExecutableQuote
from tyrex_pm.market_data.freshness import FreshnessAssessment, FreshnessReason, TimestampBasis
from tyrex_pm.signals.directional import Direction, DirectionalSignal
from tyrex_pm.strategies.context import DecisionContext
from tyrex_pm.strategies.framework_validation.reference_momentum import (
    ObserveDecisionKind,
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


def _sig(direction: Direction, corr: CorrelationId | None = None) -> DirectionalSignal:
    return DirectionalSignal(
        direction=direction,
        observed_at=TS,
        correlation_id=corr or new_correlation_id(),
        causation_id=None,
        momentum=Decimal("0.01") if direction is Direction.UP else Decimal("-0.01"),
        threshold=Decimal("0.001"),
        strength=Decimal("1"),
        selected_outcome=None,
        reason_code="TEST",
        evidence={},
    )


def _ctx(snap: DecisionSnapshot) -> DecisionContext:
    return DecisionContext(
        run_id=RunId("r"),
        mode=RuntimeMode.SHADOW,
        snapshot=snap,
        target_notional=Decimal("2"),
        max_price=Decimal("0.99"),
    )


def test_up_transition_one_intent_repeated_suppressed() -> None:
    strat = ReferenceMomentumStrategy()
    snap = _snap()
    ctx = _ctx(snap)
    r1 = strat.apply_transition(_sig(Direction.UP), ctx)
    assert len(r1.intents) == 1
    assert r1.decision.kind is ObserveDecisionKind.WOULD_ENTER_UP
    r2 = strat.apply_transition(_sig(Direction.UP), ctx)
    assert r2.intents == []
    assert r2.suppress_reason == "REPEATED_DIRECTION"


def test_down_after_flat_creates_intent() -> None:
    strat = ReferenceMomentumStrategy()
    ctx = _ctx(_snap())
    strat.apply_transition(_sig(Direction.FLAT), ctx)
    r = strat.apply_transition(_sig(Direction.DOWN), ctx)
    assert len(r.intents) == 1
    assert r.intents[0].outcome.value == "NO"


def test_flat_and_unavailable_no_entry() -> None:
    strat = ReferenceMomentumStrategy()
    ctx = _ctx(_snap())
    assert strat.apply_transition(_sig(Direction.FLAT), ctx).intents == []
    assert strat.apply_transition(_sig(Direction.UNAVAILABLE), ctx).intents == []


def test_reversal_no_intent() -> None:
    strat = ReferenceMomentumStrategy()
    ctx = _ctx(_snap())
    strat.apply_transition(_sig(Direction.UP), ctx)
    r = strat.apply_transition(_sig(Direction.DOWN), ctx)
    assert r.intents == []
    assert r.suppress_reason == "REVERSAL_NO_PORTFOLIO"
    assert r.decision.kind is ObserveDecisionKind.WOULD_ENTER_DOWN


def test_evaluate_unchanged_from_r3() -> None:
    strat = ReferenceMomentumStrategy()
    sig = _sig(Direction.UP)
    d = strat.evaluate(sig)
    assert d.kind is ObserveDecisionKind.WOULD_ENTER_UP
    assert d.reason_code == "TEST"


def test_market_reset_allows_new_intent() -> None:
    strat = ReferenceMomentumStrategy()
    ctx = _ctx(_snap())
    strat.apply_transition(_sig(Direction.UP), ctx)
    strat.reset_for_market()
    r = strat.apply_transition(_sig(Direction.UP), ctx)
    assert len(r.intents) == 1
