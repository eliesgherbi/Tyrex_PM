"""Directional signal and observe strategy semantics."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.ids import CorrelationId, MarketId, TokenId, new_correlation_id
from tyrex_pm.domain.polymarket.market import BinaryMarket, MarketStatus, make_binary_instruments
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import ExecutableQuote
from tyrex_pm.market_data.freshness import FreshnessAssessment, FreshnessReason, TimestampBasis
from tyrex_pm.signals.directional import Direction, build_directional_signal
from tyrex_pm.strategies.framework_validation.reference_momentum import (
    ObserveDecisionKind,
    ReferenceMomentumStrategy,
)


TS = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def _fresh() -> FreshnessAssessment:
    return FreshnessAssessment(
        is_fresh=True,
        age_ms=10,
        threshold_ms=1000,
        timestamp_basis=TimestampBasis.EVENT_TIME,
        reason_code=FreshnessReason.FRESH,
        observed_at=TS,
    )


def _stale() -> FreshnessAssessment:
    return FreshnessAssessment(
        is_fresh=False,
        age_ms=9999,
        threshold_ms=1000,
        timestamp_basis=TimestampBasis.EVENT_TIME,
        reason_code=FreshnessReason.STALE,
        observed_at=TS,
    )


def _snapshot(*, yes_fresh=None, no_fresh=None, ref_fresh=None) -> DecisionSnapshot:
    mid = MarketId("m1")
    yes, no = make_binary_instruments(
        market_id=mid, yes_token=TokenId("y"), no_token=TokenId("n")
    )
    market = BinaryMarket(
        market_id=mid,
        condition_id="m1",
        question="q",
        yes=yes,
        no=no,
        status=MarketStatus.ACTIVE,
    )
    quote = ExecutableQuote(
        best_bid=Decimal("0.48"),
        best_ask=Decimal("0.52"),
        mid=Decimal("0.50"),
        spread=Decimal("0.04"),
        bid_size_at_touch=Decimal("10"),
        ask_size_at_touch=Decimal("10"),
    )
    return DecisionSnapshot(
        market=market,
        yes_book=None,
        no_book=None,
        yes_quote=quote,
        no_quote=quote,
        reference=None,
        yes_freshness=yes_fresh or _fresh(),
        no_freshness=no_fresh or _fresh(),
        reference_freshness=ref_fresh or _fresh(),
        observed_at=TS,
        correlation_id=new_correlation_id(),
    )


def test_one_sided_book_unavailable() -> None:
    base = _snapshot()
    one_sided = ExecutableQuote(
        best_bid=Decimal("0.48"),
        best_ask=None,
        mid=None,
        spread=None,
        bid_size_at_touch=Decimal("10"),
        ask_size_at_touch=Decimal("0"),
    )
    snap = DecisionSnapshot(
        market=base.market,
        yes_book=None,
        no_book=None,
        yes_quote=one_sided,
        no_quote=base.no_quote,
        reference=None,
        yes_freshness=base.yes_freshness,
        no_freshness=base.no_freshness,
        reference_freshness=base.reference_freshness,
        observed_at=base.observed_at,
        correlation_id=base.correlation_id,
    )
    sig = build_directional_signal(
        snapshot=snap,
        momentum=Decimal("0.01"),
        momentum_ready=True,
        momentum_reason="OK",
        threshold=Decimal("0.001"),
        max_spread=Decimal("0.1"),
    )
    assert sig.direction is Direction.UNAVAILABLE
    assert sig.reason_code == "BOOK_ONE_SIDED_OR_EMPTY"


def test_up_down_flat_unavailable() -> None:
    up = build_directional_signal(
        snapshot=_snapshot(),
        momentum=Decimal("0.01"),
        momentum_ready=True,
        momentum_reason="OK",
        threshold=Decimal("0.001"),
        max_spread=Decimal("0.1"),
    )
    assert up.direction is Direction.UP

    down = build_directional_signal(
        snapshot=_snapshot(),
        momentum=Decimal("-0.01"),
        momentum_ready=True,
        momentum_reason="OK",
        threshold=Decimal("0.001"),
        max_spread=Decimal("0.1"),
    )
    assert down.direction is Direction.DOWN

    flat = build_directional_signal(
        snapshot=_snapshot(),
        momentum=Decimal("0.0001"),
        momentum_ready=True,
        momentum_reason="OK",
        threshold=Decimal("0.001"),
        max_spread=Decimal("0.1"),
    )
    assert flat.direction is Direction.FLAT

    bad = build_directional_signal(
        snapshot=_snapshot(ref_fresh=_stale()),
        momentum=Decimal("0.01"),
        momentum_ready=True,
        momentum_reason="OK",
        threshold=Decimal("0.001"),
        max_spread=Decimal("0.1"),
    )
    assert bad.direction is Direction.UNAVAILABLE


def test_strategy_decisions() -> None:
    strat = ReferenceMomentumStrategy()
    snap = _snapshot()
    up = build_directional_signal(
        snapshot=snap,
        momentum=Decimal("0.01"),
        momentum_ready=True,
        momentum_reason="OK",
        threshold=Decimal("0.001"),
        max_spread=Decimal("0.1"),
    )
    up_d = strat.evaluate(up)
    assert up_d.action.value == "ENTER"
    assert up_d.evidence["validation_kind"] == ObserveDecisionKind.WOULD_ENTER_UP.value

    flat = build_directional_signal(
        snapshot=snap,
        momentum=Decimal("0"),
        momentum_ready=True,
        momentum_reason="OK",
        threshold=Decimal("0.001"),
        max_spread=Decimal("0.1"),
    )
    flat_d = strat.evaluate(flat)
    assert flat_d.action.value == "HOLD"
    assert flat_d.evidence["validation_kind"] == ObserveDecisionKind.HOLD.value

    skip = build_directional_signal(
        snapshot=_snapshot(ref_fresh=_stale()),
        momentum=None,
        momentum_ready=False,
        momentum_reason="STALE",
        threshold=Decimal("0.001"),
        max_spread=Decimal("0.1"),
    )
    skip_d = strat.evaluate(skip)
    assert skip_d.action.value == "SKIP"
    assert skip_d.evidence["validation_kind"] == ObserveDecisionKind.SKIP.value
