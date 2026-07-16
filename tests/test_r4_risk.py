"""R4 risk policy and engine tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tyrex_pm.core.ids import InstrumentId, MarketId, TokenId, new_correlation_id, new_event_id
from tyrex_pm.core.intents import EnterIntent, IntentId, new_intent_id
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.core.snapshots import BookLevel, BookSnapshot
from tyrex_pm.domain.polymarket.market import BinaryMarket, MarketStatus, make_binary_instruments
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import book_quote
from tyrex_pm.market_data.freshness import FreshnessAssessment, FreshnessReason, TimestampBasis
from tyrex_pm.risk.context import BookReadiness, RiskConfigView, RiskContext
from tyrex_pm.risk.dedup import IntentDedupRegistry
from tyrex_pm.risk.engine import RiskEngine
from tyrex_pm.risk.policies import (
    DuplicateIntentPolicy,
    KillSwitchPolicy,
    RuntimeModePolicy,
)
from tyrex_pm.risk.reasons import RiskReason
from tyrex_pm.strategies.framework_validation.reference_momentum import ReferenceMomentumStrategy

TS = datetime(2026, 7, 16, 12, 1, 0, tzinfo=timezone.utc)


def _fresh(reason: FreshnessReason = FreshnessReason.FRESH, ok: bool = True) -> FreshnessAssessment:
    return FreshnessAssessment(
        is_fresh=ok,
        age_ms=10,
        threshold_ms=1000,
        timestamp_basis=TimestampBasis.EVENT_TIME,
        reason_code=reason,
        observed_at=TS,
    )


def _market(**kwargs) -> BinaryMarket:
    mid = MarketId("m1")
    yes, no = make_binary_instruments(market_id=mid, yes_token=TokenId("y"), no_token=TokenId("n"))
    return BinaryMarket(
        market_id=mid,
        condition_id="m1",
        question="q",
        yes=yes,
        no=no,
        status=kwargs.get("status", MarketStatus.ACTIVE),
        event_end=kwargs.get("event_end", TS + timedelta(minutes=5)),
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("1"),
    )


def _book(instrument: str) -> BookSnapshot:
    return BookSnapshot(
        instrument_id=InstrumentId(instrument),
        ts_event=TS,
        bids=(BookLevel(price=Decimal("0.48"), quantity=Decimal("100")),),
        asks=(BookLevel(price=Decimal("0.52"), quantity=Decimal("100")),),
    )


def _ctx(
    *,
    mode: RuntimeMode = RuntimeMode.SHADOW,
    kill: bool = False,
    stale: bool = False,
    dedup: IntentDedupRegistry | None = None,
    market: BinaryMarket | None = None,
) -> RiskContext:
    market = market or _market()
    yes_book = _book(market.yes.instrument_id.value)
    no_book = _book(market.no.instrument_id.value)
    fresh = _fresh(FreshnessReason.STALE, ok=False) if stale else _fresh()
    snap = DecisionSnapshot(
        market=market,
        yes_book=yes_book,
        no_book=no_book,
        yes_quote=book_quote(yes_book),
        no_quote=book_quote(no_book),
        reference=None,
        yes_freshness=fresh,
        no_freshness=fresh,
        reference_freshness=fresh,
        observed_at=TS,
        correlation_id=new_correlation_id(),
    )
    return RiskContext(
        mode=mode,
        now=TS,
        market=market,
        snapshot=snap,
        yes_quote=snap.yes_quote,
        no_quote=snap.no_quote,
        yes_book=BookReadiness(initialized=True, recovery_required=False, tick_size=Decimal("0.01")),
        no_book=BookReadiness(initialized=True, recovery_required=False, tick_size=Decimal("0.01")),
        risk_config=RiskConfigView(
            max_notional=Decimal("5"),
            min_price=Decimal("0.01"),
            max_price=Decimal("0.99"),
            max_spread=Decimal("0.10"),
            min_liquidity_notional=Decimal("1"),
            no_entry_before_close=timedelta(seconds=30),
            kill_switch_active=kill,
            config_fingerprint="fp",
        ),
        dedup=dedup or IntentDedupRegistry(lifetime=timedelta(hours=1)),
        exposure_available=False,
    )


def _intent(market: BinaryMarket | None = None, *, notional: str = "2") -> EnterIntent:
    market = market or _market()
    return EnterIntent(
        intent_id=new_intent_id(),
        strategy_id=ReferenceMomentumStrategy.STRATEGY_ID,
        instrument_id=market.yes.instrument_id,
        market_id=market.market_id,
        created_at=TS,
        correlation_id=new_correlation_id(),
        causation_id=new_event_id(),
        reason_code="MOMENTUM_UP",
        target_notional=Decimal(notional),
        outcome=OutcomeSide.YES,
        decision_epoch=1,
        max_price=Decimal("0.99"),
    )


def test_approve_shadow() -> None:
    eng = RiskEngine()
    d = eng.evaluate(_intent(), _ctx())
    assert d.approved
    assert RiskReason.APPROVED in d.reason_codes
    assert d.evidence["exposure_available"] is False


def test_live_tiny_denied() -> None:
    d = RiskEngine().evaluate(_intent(), _ctx(mode=RuntimeMode.LIVE_TINY))
    assert not d.approved
    assert RiskReason.LIVE_NOT_SUPPORTED in d.reason_codes


def test_kill_switch() -> None:
    d = RiskEngine().evaluate(_intent(), _ctx(kill=True))
    assert RiskReason.KILL_SWITCH_ACTIVE in d.reason_codes


def test_stale_denied() -> None:
    d = RiskEngine().evaluate(_intent(), _ctx(stale=True))
    assert RiskReason.DATA_STALE in d.reason_codes


def test_duplicate() -> None:
    dedup = IntentDedupRegistry(lifetime=timedelta(hours=1))
    ctx = _ctx(dedup=dedup)
    i1 = _intent()
    assert RiskEngine().evaluate(i1, ctx).approved
    i2 = EnterIntent(
        intent_id=IntentId("other"),
        strategy_id=i1.strategy_id,
        instrument_id=i1.instrument_id,
        market_id=i1.market_id,
        created_at=TS,
        correlation_id=i1.correlation_id,
        causation_id=i1.causation_id,
        reason_code=i1.reason_code,
        target_notional=i1.target_notional,
        outcome=i1.outcome,
        decision_epoch=i1.decision_epoch,
        max_price=i1.max_price,
    )
    assert i1.semantic_key() == i2.semantic_key()
    d2 = RiskEngine().evaluate(i2, ctx)
    assert RiskReason.DUPLICATE_INTENT in d2.reason_codes


def test_notional_cap() -> None:
    d = RiskEngine().evaluate(_intent(notional="10"), _ctx())
    assert RiskReason.NOTIONAL_LIMIT_EXCEEDED in d.reason_codes


def test_entry_window_closed() -> None:
    market = _market(event_end=TS + timedelta(seconds=10))
    d = RiskEngine().evaluate(_intent(market), _ctx(market=market))
    assert RiskReason.ENTRY_WINDOW_CLOSED in d.reason_codes


def test_policy_exception_fails_closed() -> None:
    class Boom:
        policy_id = "boom"

        def evaluate(self, intent, context):
            raise RuntimeError("boom")

    eng = RiskEngine(policies=(Boom(),))
    d = eng.evaluate(_intent(), _ctx())
    assert not d.approved
    assert RiskReason.INTERNAL_POLICY_ERROR in d.reason_codes


def test_mode_policy_observe_allows_dry() -> None:
    r = RuntimeModePolicy().evaluate(_intent(), _ctx(mode=RuntimeMode.OBSERVE))
    assert r.approved


def test_deterministic_order_first_failures_recorded() -> None:
    d = RiskEngine().evaluate(_intent(), _ctx(mode=RuntimeMode.LIVE_TINY, kill=True))
    ids = [p.policy_id for p in d.policy_results]
    assert ids.index("runtime_mode") < ids.index("kill_switch")
