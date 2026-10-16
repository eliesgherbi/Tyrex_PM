"""settlement_value: schema, economics and driver episode/readiness behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import InstrumentId, MarketId, RunId, TokenId, new_correlation_id
from tyrex_pm.core.instruments import Instrument, OutcomeSide
from tyrex_pm.core.intents import EnterIntent, HoldToResolutionIntent
from tyrex_pm.core.snapshots import BookLevel, BookSnapshot, ReferencePriceSnapshot
from tyrex_pm.core.time_authority import FakeTimeAuthority
from tyrex_pm.domain.polymarket.fees import FeeCurveParams
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.domain.polymarket.ptb import PtbQuality, PtbSnapshot, PtbSourceClass
from tyrex_pm.indicators.ewma_volatility import SIGMA_UNITS_PER_SQRT_SECOND, VolatilitySnapshot
from tyrex_pm.indicators.graph import FeatureBundle
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import book_quote
from tyrex_pm.market_data.freshness import FreshnessAssessment, FreshnessReason, TimestampBasis
from tyrex_pm.runtime.config_error import ConfigError
from tyrex_pm.strategies.context import (
    DecisionContext,
    ReferenceFacts,
    StrategyLifecycleSnapshot,
    StrategyPositionPhase,
)
from tyrex_pm.strategies.decisions import StrategyAction
from tyrex_pm.strategies.registry import get_strategy_plugin
from tyrex_pm.strategies.settlement_value import reasons as R
from tyrex_pm.strategies.settlement_value.config import (
    SettlementEntryConfig,
    SettlementModelConfig,
    SettlementSizingConfig,
)
from tyrex_pm.strategies.settlement_value.driver import create_settlement_value_driver
from tyrex_pm.strategies.settlement_value.economics import (
    choose_side,
    endpoint_probability,
    size_side,
)
from tyrex_pm.strategies.settlement_value.schema import settlement_value_config_from_parameters

START = datetime(2026, 10, 6, 17, 50, tzinfo=timezone.utc)
END = START + timedelta(minutes=5)
NO_FEE = FeeCurveParams(fee_rate=Decimal("0"), exponent=Decimal("1"))


def lv(price: str, qty: str) -> BookLevel:
    return BookLevel(price=Decimal(price), quantity=Decimal(qty))


# -- schema / plugin ---------------------------------------------------------------


def test_schema_rejects_unknown_keys_and_disabled_backend() -> None:
    with pytest.raises(ConfigError):
        settlement_value_config_from_parameters({"entry": {"bogus": 1}})
    with pytest.raises(ConfigError):
        settlement_value_config_from_parameters({"unknown_section": {}})
    with pytest.raises(ConfigError, match="disabled"):
        settlement_value_config_from_parameters({"model": {"backend": "average_diffusion"}})
    with pytest.raises(ConfigError):
        settlement_value_config_from_parameters({"model": {"spot_input": "perp"}})
    cfg = settlement_value_config_from_parameters(
        {"entry": {"min_edge_per_share": "0.03", "target_mode": "require_target"}}
    )
    assert cfg.entry.min_edge_per_share == Decimal("0.03")


def test_plugin_requires_hold_lifecycle_and_budget_constrained_sizing() -> None:
    from tyrex_pm.strategies.registry import plugin_lifecycle_modes

    plugin = get_strategy_plugin("settlement_value")
    cfg = plugin.load_config({}, "test")
    assert plugin_lifecycle_modes(plugin, cfg) == frozenset({"hold_to_resolution"})
    assert plugin.validate_run_config is not None
    plugin.validate_run_config(cfg)  # cap_only default is accepted
    target = plugin.load_config({"entry": {"target_mode": "require_target"}}, "test")
    with pytest.raises(ValueError, match="cap_only"):
        plugin.validate_run_config(target)


# -- economics -------------------------------------------------------------------


def test_down_side_conservative_probability_uses_one_minus_p_high() -> None:
    prob = endpoint_probability(
        S=Decimal("100.05"), K=Decimal("100"), sigma=1e-4, tau_s=120.0,
        model=SettlementModelConfig(sigma_band=0.25, reference_band_bps=1.0),
    )
    assert prob.ready
    assert prob.p_up_low < prob.p_up_point < prob.p_up_high
    assert prob.conservative(up=True) == prob.p_up_low
    assert prob.conservative(up=False) == pytest.approx(1.0 - prob.p_up_high)


def test_cap_only_stops_at_debit_cap_and_min_edge() -> None:
    sizing = SettlementSizingConfig(max_debit_usdc=Decimal("5"), max_loss_usdc=Decimal("5"))
    entry = SettlementEntryConfig(min_edge_per_share=Decimal("0.05"))
    cand = size_side(
        [lv("0.50", "4"), lv("0.55", "100")],
        up=True, p_conservative=0.70, p_point=0.72, entry=entry, sizing=sizing,
        fee_curve=NO_FEE, min_order_shares=Decimal("5"),
    )
    assert cand.feasible and cand.binding_constraint == "max_debit"
    assert cand.debit <= Decimal("5")
    assert cand.worst_price == Decimal("0.55")
    # Marginal edge at 0.66 is 0.04 < 0.05: the walk stops before that level.
    stopped = size_side(
        [lv("0.60", "20"), lv("0.66", "20")],
        up=True, p_conservative=0.70, p_point=0.70, entry=entry, sizing=sizing,
        fee_curve=NO_FEE, min_order_shares=Decimal("1"),
    )
    assert stopped.worst_price == Decimal("0.60")
    assert stopped.gross_shares == Decimal("8.33")


def test_require_target_smallest_size_or_infeasible() -> None:
    entry = SettlementEntryConfig(
        min_edge_per_share=Decimal("0"), target_mode="require_target",
        target_profit_usdc=Decimal("1"),
    )
    sizing = SettlementSizingConfig(max_debit_usdc=Decimal("5"), max_loss_usdc=Decimal("5"))
    cand = size_side(
        [lv("0.50", "100")], up=True, p_conservative=0.60, p_point=0.60, entry=entry,
        sizing=sizing, fee_curve=NO_FEE, min_order_shares=Decimal("1"),
    )
    assert cand.feasible and cand.binding_constraint == "target_reached"
    assert cand.gross_shares == Decimal("10")  # 10 * (0.60 - 0.50) = 1.00
    small = size_side(
        [lv("0.50", "100")], up=True, p_conservative=0.52, p_point=0.52, entry=entry,
        sizing=sizing, fee_curve=NO_FEE, min_order_shares=Decimal("1"),
    )
    assert not small.feasible and small.reason == R.TARGET_INFEASIBLE


def test_share_fees_reduce_net_shares_and_minimum_size_applies() -> None:
    curve = FeeCurveParams(fee_rate=Decimal("0.07"), exponent=Decimal("1"))
    entry = SettlementEntryConfig(min_edge_per_share=Decimal("0"))
    sizing = SettlementSizingConfig()
    cand = size_side(
        [lv("0.50", "4")], up=True, p_conservative=0.9, p_point=0.9, entry=entry,
        sizing=sizing, fee_curve=curve, min_order_shares=Decimal("5"),
    )
    assert cand.net_shares < cand.gross_shares
    assert not cand.feasible and cand.reason == R.BELOW_MINIMUM_SIZE
    none = size_side(
        [lv("0.95", "100")], up=True, p_conservative=0.9, p_point=0.9, entry=entry,
        sizing=sizing, fee_curve=curve, min_order_shares=Decimal("5"),
    )
    assert none.reason == R.EDGE_BELOW_COST


def test_side_choice_is_deterministic() -> None:
    entry = SettlementEntryConfig(min_edge_per_share=Decimal("0"))
    sizing = SettlementSizingConfig()
    kwargs = dict(entry=entry, sizing=sizing, fee_curve=NO_FEE, min_order_shares=Decimal("1"))
    up = size_side([lv("0.40", "100")], up=True, p_conservative=0.5, p_point=0.5, **kwargs)
    down = size_side([lv("0.40", "100")], up=False, p_conservative=0.5, p_point=0.5, **kwargs)
    assert choose_side(up, down) is up
    assert choose_side(down, up) is up


# -- driver ------------------------------------------------------------------------


def market() -> BinaryMarket:
    mid = MarketId("m")
    return BinaryMarket(
        market_id=mid,
        condition_id="c",
        question="q",
        yes=Instrument(instrument_id=InstrumentId("up"), market_id=mid,
                       token_id=TokenId("up"), outcome=OutcomeSide.YES),
        no=Instrument(instrument_id=InstrumentId("down"), market_id=mid,
                      token_id=TokenId("down"), outcome=OutcomeSide.NO),
        event_start=START,
        event_end=END,
    )


def fresh(ok: bool = True) -> FreshnessAssessment:
    return FreshnessAssessment(
        is_fresh=ok, age_ms=0, threshold_ms=5000, timestamp_basis=TimestampBasis.EVENT_TIME,
        reason_code=FreshnessReason.FRESH if ok else FreshnessReason.STALE, observed_at=START,
    )


def snapshot(at: datetime, *, up_ask="0.30", down_ask="0.75") -> DecisionSnapshot:
    def bid(ask: str) -> str:
        return str(Decimal(ask) - Decimal("0.01"))

    up = BookSnapshot(instrument_id=InstrumentId("up"), ts_event=at,
                      bids=(lv(bid(up_ask), "100"),), asks=(lv(up_ask, "100"),))
    down = BookSnapshot(instrument_id=InstrumentId("down"), ts_event=at,
                        bids=(lv(bid(down_ask), "100"),), asks=(lv(down_ask, "100"),))
    return DecisionSnapshot(
        market=market(), yes_book=up, no_book=down, yes_quote=book_quote(up),
        no_quote=book_quote(down),
        reference=ReferencePriceSnapshot(symbol="X", price=Decimal("100000"), ts_event=at),
        yes_freshness=fresh(), no_freshness=fresh(), reference_freshness=fresh(),
        observed_at=at, correlation_id=new_correlation_id(),
    )


def ptb(quality=PtbQuality.PROVISIONAL, k="100000") -> PtbSnapshot:
    return PtbSnapshot(
        market_id=MarketId("m"), window_id="w", event_start=START, event_end=END,
        k=Decimal(k), source_class=PtbSourceClass.SETTLEMENT_BOUNDARY, source_ts=START,
        receive_ts=START, boundary_lag_ms=0, quality=quality,
    )


def vol(ready: bool = True) -> FeatureBundle:
    return FeatureBundle(values={"ewma_vol": VolatilitySnapshot(
        sigma=1e-4, sigma_units=SIGMA_UNITS_PER_SQRT_SECOND, ready=ready, sample_count=100,
        effective_samples_s=60.0, last_update_ts=START, jump_guard_tripped=False,
        reject_reason=None if ready else "warming_up",
    )})


def context(snap: DecisionSnapshot, *, qty="0", allowed=True, spot="100000") -> DecisionContext:
    return DecisionContext(
        run_id=RunId("t"), snapshot=snap, target_notional=Decimal("5"),
        lifecycle=StrategyLifecycleSnapshot(
            phase=StrategyPositionPhase.ACTIVE if Decimal(qty) else StrategyPositionPhase.FLAT,
            instrument_id=InstrumentId("up") if Decimal(qty) else None,
        ),
        position_quantity=Decimal(qty), entry_allowed=allowed,
        references=ReferenceFacts(sealed_k=Decimal("100000"),
                                  trading_reference_raw=Decimal(spot)),
    )


def driver(**params):
    base = {"quality": {"accepted_k_qualities": ["PROVISIONAL", "CONFIRMED_CANONICAL"]}}
    base.update(params)
    clock = FakeClock(_wall=START)
    d = create_settlement_value_driver(
        config=settlement_value_config_from_parameters(base),
        time_authority=FakeTimeAuthority(clock=clock),
        window_id="w",
    )
    d.configure_ptb(ptb())
    return d


def run(d, at, ctx_kwargs=None, features=None, **snap_kwargs):
    snap = snapshot(at, **snap_kwargs)
    return d.evaluate(
        market_snapshot=snap, causation_id=None, correlation_id=snap.correlation_id,
        context=context(snap, **(ctx_kwargs or {})), trigger="test",
        features=vol() if features is None else features,
    )


def test_driver_enters_once_then_waits_for_the_episode() -> None:
    d = driver()
    at = START + timedelta(seconds=120)  # tau = 180 s, S = K -> p_up ~ 0.5
    first = run(d, at)
    assert first.decision.action is StrategyAction.ENTER
    (intent,) = first.intents
    assert isinstance(intent, EnterIntent)
    assert intent.instrument_id == InstrumentId("up")  # Up ask 0.30 vs ~0.5
    assert intent.max_price == Decimal("0.30")
    second = run(d, at + timedelta(seconds=1), ctx_kwargs={"allowed": False})
    assert second.decision.reason_code == R.PENDING_ORDER and not second.intents
    third = run(d, at + timedelta(seconds=2))
    assert third.decision.reason_code == R.EPISODE_CONSUMED and not third.intents


def test_partial_fill_requests_hold_once_and_never_tops_up() -> None:
    d = driver()
    at = START + timedelta(seconds=120)
    run(d, at)
    held = run(d, at + timedelta(seconds=1), ctx_kwargs={"qty": "3"})
    assert held.decision.action is StrategyAction.HOLD
    assert [type(i) for i in held.intents] == [HoldToResolutionIntent]
    again = run(d, at + timedelta(seconds=2), ctx_kwargs={"qty": "3"})
    assert again.decision.reason_code == R.HOLDING and not again.intents
    # Model state is still recorded while holding (comparable decision records).
    assert again.reporting_context["decision_row"]["p_up_point"] is not None


@pytest.mark.parametrize(
    ("offset_s", "reason"),
    [(30, R.OUTSIDE_ENTRY_BAND), (290, R.ENTRY_WINDOW_CLOSED)],
)
def test_entry_band(offset_s: int, reason: str) -> None:
    result = run(driver(), START + timedelta(seconds=offset_s))
    assert result.decision.reason_code == reason and not result.intents


def test_k_quality_and_volatility_readiness_block_entry() -> None:
    d = driver(quality={"accepted_k_qualities": ["CONFIRMED_CANONICAL"]})
    at = START + timedelta(seconds=120)
    assert run(d, at).decision.reason_code == R.K_NOT_ACCEPTED
    d2 = driver()
    assert run(d2, at, features=vol(ready=False)).decision.reason_code == R.MODEL_NOT_READY
    d3 = driver()
    d3.ptb = None
    assert run(d3, at).decision.reason_code == R.K_MISSING


def test_no_edge_waits_with_reason() -> None:
    result = run(driver(), START + timedelta(seconds=120), up_ask="0.55", down_ask="0.55")
    assert result.decision.action is StrategyAction.WAIT
    assert result.decision.reason_code == R.EDGE_BELOW_COST
    row = result.reporting_context["decision_row"]
    assert row["up_feasible"] is False and row["down_feasible"] is False
