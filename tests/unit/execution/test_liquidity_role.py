"""LiquidityRole on EnterIntent and HoldToResolution consumption."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.ids import CorrelationId, InstrumentId, MarketId, StrategyId
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.intents import EnterIntent, HoldToResolutionIntent, LiquidityRole, new_intent_id
from tyrex_pm.execution.orders import LimitOrderSpec, MarketBuyOrderSpec, TimeInForce
from tyrex_pm.execution.planner import ExecutionRiskPolicy, IntentOrderPlanner
from tyrex_pm.runtime.market_family import get_market_family, registered_market_families

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def _enter(*, role: LiquidityRole) -> EnterIntent:
    return EnterIntent(
        intent_id=new_intent_id(),
        strategy_id=StrategyId("guide"),
        instrument_id=InstrumentId("yes"),
        market_id=MarketId("market"),
        created_at=NOW,
        correlation_id=CorrelationId("c1"),
        causation_id=None,
        reason_code="ENTRY",
        target_notional=Decimal("5"),
        outcome=OutcomeSide.YES,
        max_price=Decimal("0.51"),
        liquidity_role=role,
    )


def test_taker_entry_plans_fak_market_buy() -> None:
    planner = IntentOrderPlanner(ExecutionRiskPolicy(maximum_total_debit=Decimal("5")))
    spec = planner.entry(_enter(role=LiquidityRole.TAKER), token_id="tok")
    assert isinstance(spec, MarketBuyOrderSpec)
    assert spec.time_in_force is TimeInForce.FAK


def test_maker_entry_plans_gtc_limit() -> None:
    planner = IntentOrderPlanner(ExecutionRiskPolicy(maximum_total_debit=Decimal("5")))
    spec = planner.entry(_enter(role=LiquidityRole.MAKER), token_id="tok")
    assert isinstance(spec, LimitOrderSpec)
    assert spec.time_in_force is TimeInForce.GTC
    assert spec.side.value == "BUY"


def test_hold_to_resolution_intent_is_constructible() -> None:
    intent = HoldToResolutionIntent(
        intent_id=new_intent_id(),
        strategy_id=StrategyId("z_gap"),
        instrument_id=InstrumentId("yes"),
        market_id=MarketId("market"),
        created_at=NOW,
        correlation_id=CorrelationId("c1"),
        causation_id=None,
        reason_code="HOLD_TO_RESOLUTION",
        window_id="btc-updown-5m",
    )
    assert "HOLD_TO_RESOLUTION" in intent.semantic_key()


def test_btc_updown_5m_is_registered_market_family() -> None:
    assert "btc_updown_5m" in registered_market_families()
    family = get_market_family("btc_updown_5m")
    assert family.window_duration_s == 300
