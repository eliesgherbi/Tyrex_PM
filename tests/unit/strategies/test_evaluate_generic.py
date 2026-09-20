"""F3: evaluate() is strategy-neutral; Chainlink rides on ReferenceFacts."""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import CorrelationId, EventId, InstrumentId, MarketId, RunId, TokenId
from tyrex_pm.core.instruments import Instrument, OutcomeSide
from tyrex_pm.core.snapshots import ReferencePriceSnapshot
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.indicators.graph import FeatureBundle
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import ExecutableQuote
from tyrex_pm.market_data.freshness import FreshnessAssessment, FreshnessReason, TimestampBasis
from tyrex_pm.strategies.ask70.config import Ask70Config
from tyrex_pm.strategies.ask70.driver import Ask70Driver, create_ask70_driver
from tyrex_pm.strategies.context import DecisionContext, ReferenceFacts
from tyrex_pm.strategies.z_gap.config import ZGapConfig
from tyrex_pm.strategies.z_gap.driver import ZGapDriver, create_z_gap_driver


def _fresh() -> FreshnessAssessment:
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    return FreshnessAssessment(
        is_fresh=True,
        age_ms=0,
        threshold_ms=5_000,
        timestamp_basis=TimestampBasis.EVENT_TIME,
        reason_code=FreshnessReason.FRESH,
        observed_at=now,
    )


def _quote(ask: Decimal | None) -> ExecutableQuote:
    return ExecutableQuote(
        best_ask=ask,
        best_bid=None,
        mid=None,
        spread=None,
        ask_size_at_touch=Decimal("10") if ask is not None else Decimal("0"),
        bid_size_at_touch=Decimal("0"),
    )


def _market() -> BinaryMarket:
    yes = Instrument(
        market_id=MarketId("m1"),
        token_id=TokenId("yes"),
        instrument_id=InstrumentId("yes"),
        outcome=OutcomeSide.YES,
    )
    no = Instrument(
        market_id=MarketId("m1"),
        token_id=TokenId("no"),
        instrument_id=InstrumentId("no"),
        outcome=OutcomeSide.NO,
    )
    return BinaryMarket(
        market_id=MarketId("m1"),
        condition_id="condition-1",
        question="f3 evaluate",
        yes=yes,
        no=no,
        event_start=datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc),
        event_end=datetime(2026, 9, 13, 12, 5, tzinfo=timezone.utc),
    )


def _snapshot(*, price: Decimal | None = Decimal("64000")) -> DecisionSnapshot:
    now = datetime(2026, 9, 13, 12, 1, tzinfo=timezone.utc)
    ref = None
    if price is not None:
        ref = ReferencePriceSnapshot(
            symbol="BTCUSD_ALIGNED",
            price=price,
            ts_event=now,
            venue="aligned_estimate",
        )
    return DecisionSnapshot(
        market=_market(),
        yes_book=None,
        no_book=None,
        yes_quote=_quote(Decimal("0.51")),
        no_quote=_quote(Decimal("0.49")),
        reference=ref,
        yes_freshness=_fresh(),
        no_freshness=_fresh(),
        reference_freshness=_fresh(),
        observed_at=now,
        correlation_id=CorrelationId("c1"),
        causation_id=EventId("e1"),
    )


def _clock() -> FakeClock:
    return FakeClock(_wall=datetime(2026, 9, 13, 12, 1, tzinfo=timezone.utc))


def test_evaluate_signatures_have_no_settlement_kwargs() -> None:
    for cls in (Ask70Driver, ZGapDriver):
        params = inspect.signature(cls.evaluate).parameters
        assert "settlement_reference" not in params
        assert "settlement_reference_fresh" not in params
        assert "features" in params
        assert "context" in params


def test_reference_facts_default_empty() -> None:
    snap = _snapshot()
    ctx = DecisionContext(run_id=RunId("t"), snapshot=snap, target_notional=Decimal("5"))
    assert ctx.references.chainlink_raw is None
    assert ctx.references.chainlink_fresh is False
    assert ctx.references.sealed_k is None


def test_z_gap_evaluate_maps_reference_facts_to_settlement_ref() -> None:
    driver = create_z_gap_driver(config=ZGapConfig(), clock=_clock())
    snap = _snapshot(price=Decimal("64000"))
    ctx = DecisionContext(
        run_id=RunId("z"),
        snapshot=snap,
        target_notional=Decimal("5"),
        now=snap.observed_at,
        references=ReferenceFacts(
            chainlink_raw=Decimal("65000"),
            chainlink_fresh=True,
            sealed_k=Decimal("64100"),
        ),
    )
    result = driver.evaluate(
        market_snapshot=snap,
        causation_id=snap.causation_id,
        correlation_id=snap.correlation_id,
        context=ctx,
        trigger="feed",
        features=FeatureBundle(),
    )
    decision_input = result.reporting_context["decision_input"]
    assert decision_input.model.basis_bps is not None
    assert decision_input.model.basis_bps != Decimal("0")


def test_z_gap_evaluate_without_reference_facts_uses_s_vs_s() -> None:
    driver = create_z_gap_driver(config=ZGapConfig(), clock=_clock())
    snap = _snapshot(price=Decimal("64000"))
    ctx = DecisionContext(
        run_id=RunId("z"),
        snapshot=snap,
        target_notional=Decimal("5"),
        now=snap.observed_at,
    )
    result = driver.evaluate(
        market_snapshot=snap,
        causation_id=snap.causation_id,
        correlation_id=snap.correlation_id,
        context=ctx,
        trigger="feed",
        features=FeatureBundle(),
    )
    decision_input = result.reporting_context["decision_input"]
    assert decision_input.model.basis_bps == Decimal("0")


def test_ask70_evaluate_ignores_empty_bundle_and_references() -> None:
    driver = create_ask70_driver(
        config=Ask70Config(
            entry_ask_threshold=Decimal("0.70"),
            leg_preference="first_hit",
            max_price_pad=Decimal("0.08"),
            tau_min_s=10.0,
            tau_max_s=290.0,
            max_clock_uncertainty_ms=750.0,
        ),
        clock=_clock(),
    )
    snap = _snapshot(price=None)
    ctx = DecisionContext(
        run_id=RunId("a"),
        snapshot=snap,
        target_notional=Decimal("5"),
        now=snap.observed_at,
        entry_allowed=True,
    )
    result = driver.evaluate(
        market_snapshot=snap,
        causation_id=snap.causation_id,
        correlation_id=snap.correlation_id,
        context=ctx,
        trigger="feed",
        features=FeatureBundle(),
    )
    assert result.decision is not None
    assert result.eligibility is not None
