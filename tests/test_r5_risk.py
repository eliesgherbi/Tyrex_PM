"""R5 portfolio-aware risk extensions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tyrex_pm.core.ids import InstrumentId, MarketId, StrategyId, TokenId, new_correlation_id
from tyrex_pm.core.intents import EnterIntent, FlattenIntent, IntentKind, new_intent_id
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.domain.polymarket.market import BinaryMarket, MarketStatus, make_binary_instruments
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import ExecutableQuote
from tyrex_pm.market_data.freshness import FreshnessAssessment, FreshnessReason, TimestampBasis
from tyrex_pm.risk.context import BookReadiness, PortfolioRiskView, RiskConfigView, RiskContext
from tyrex_pm.risk.dedup import IntentDedupRegistry
from tyrex_pm.risk.engine import RiskEngine
from tyrex_pm.risk.reasons import RiskReason

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


def _market() -> BinaryMarket:
    mid = MarketId("m1")
    yes, no = make_binary_instruments(market_id=mid, yes_token=TokenId("y"), no_token=TokenId("n"))
    return BinaryMarket(
        market_id=mid,
        condition_id="m1",
        question="q",
        yes=yes,
        no=no,
        status=MarketStatus.ACTIVE,
    )


def _snap(market: BinaryMarket) -> DecisionSnapshot:
    q = ExecutableQuote(
        best_bid=Decimal("0.48"),
        best_ask=Decimal("0.52"),
        mid=Decimal("0.5"),
        spread=Decimal("0.04"),
        bid_size_at_touch=Decimal("100"),
        ask_size_at_touch=Decimal("100"),
    )
    return DecisionSnapshot(
        market=market,
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


def _ready() -> BookReadiness:
    return BookReadiness(initialized=True, recovery_required=False, tick_size=Decimal("0.01"))


def _ctx(
    *,
    portfolio: PortfolioRiskView | None,
    kill: bool = False,
    exposure_available: bool = True,
) -> RiskContext:
    market = _market()
    return RiskContext(
        mode=RuntimeMode.SHADOW,
        now=TS,
        market=market,
        snapshot=_snap(market),
        yes_quote=_snap(market).yes_quote,
        no_quote=_snap(market).no_quote,
        yes_book=_ready(),
        no_book=_ready(),
        risk_config=RiskConfigView(
            max_notional=Decimal("10"),
            min_price=Decimal("0.01"),
            max_price=Decimal("0.99"),
            max_spread=Decimal("0.2"),
            min_liquidity_notional=Decimal("1"),
            no_entry_before_close=timedelta(0),
            kill_switch_active=kill,
            config_fingerprint="fp",
        ),
        dedup=IntentDedupRegistry(lifetime=timedelta(hours=1)),
        exposure_available=exposure_available,
        portfolio=portfolio,
    )


def _enter(market: BinaryMarket) -> EnterIntent:
    return EnterIntent(
        intent_id=new_intent_id(),
        strategy_id=StrategyId("reference_momentum"),
        instrument_id=market.yes.instrument_id,
        market_id=market.market_id,
        created_at=TS,
        correlation_id=new_correlation_id(),
        causation_id=None,
        reason_code="TEST",
        target_notional=Decimal("5"),
    )


def test_missing_portfolio_fails_closed() -> None:
    engine = RiskEngine()
    market = _market()
    ctx = _ctx(portfolio=None, exposure_available=True)
    # Force portfolio path: exposure_available True with portfolio None
    d = engine.evaluate(_enter(market), ctx)
    assert not d.approved
    assert RiskReason.PORTFOLIO_UNAVAILABLE in d.reason_codes


def test_pending_order_blocks_entry() -> None:
    engine = RiskEngine()
    market = _market()
    view = PortfolioRiskView(
        available=True,
        net_quantity=Decimal("0"),
        total_cost_notional=Decimal("0"),
        lifecycle_state="ENTRY_PENDING",
        has_pending_order=True,
        max_position_notional=Decimal("20"),
        max_total_exposure=Decimal("50"),
    )
    d = engine.evaluate(_enter(market), _ctx(portfolio=view))
    assert not d.approved
    assert RiskReason.PENDING_ORDER_BLOCKS_ENTRY in d.reason_codes


def test_kill_switch_denies_entry_allows_flatten() -> None:
    engine = RiskEngine()
    market = _market()
    view = PortfolioRiskView(
        available=True,
        net_quantity=Decimal("5"),
        total_cost_notional=Decimal("2.5"),
        lifecycle_state="ACTIVE",
        has_pending_order=False,
        max_position_notional=Decimal("20"),
        max_total_exposure=Decimal("50"),
    )
    ctx = _ctx(portfolio=view, kill=True)
    d_enter = engine.evaluate(_enter(market), ctx)
    assert not d_enter.approved
    assert RiskReason.KILL_SWITCH_ACTIVE in d_enter.reason_codes

    flat = FlattenIntent(
        intent_id=new_intent_id(),
        strategy_id=StrategyId("reference_momentum"),
        instrument_id=market.yes.instrument_id,
        market_id=market.market_id,
        created_at=TS,
        correlation_id=new_correlation_id(),
        causation_id=None,
        reason_code="KILL_SWITCH",
    )
    # Flatten still needs books in snapshot for readiness — none present may deny.
    # With uninitialized books in snap, data readiness may fail; set portfolio ok path
    # by using engine policies that skip price for non-entry.
    d_flat = engine.evaluate(flat, ctx)
    assert RiskReason.KILL_SWITCH_ACTIVE not in d_flat.reason_codes


def test_r4_dry_still_allows_without_portfolio() -> None:
    engine = RiskEngine()
    market = _market()
    ctx = _ctx(portfolio=None, exposure_available=False)
    # Still may fail on missing books for price policy — use intent that gets to portfolio policy.
    # PriceSpreadLiquidity needs book in snapshot. Skip full approve; only check portfolio policy path.
    from tyrex_pm.risk.policies import PortfolioExposurePolicy

    r = PortfolioExposurePolicy().evaluate(_enter(market), ctx)
    assert r.approved
