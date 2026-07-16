"""REST fallback policy enforcement tests (M8 D3)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.market_data.decision_gate import should_block_paired_binary_decision
from tyrex_pm.market_data.models import BookLevel, BookSource
from tyrex_pm.market_data.quality import (
    CRYPTO_5M_PROFILE,
    DataQualityGate,
    DataQualityGateConfig,
    DecisionContext,
    EnforcementMode,
    QualityVerdict,
)
from tyrex_pm.runtime.config import PairedBinaryStrategyConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.state.market_store import MarketStateStore
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"


def _gate(**kw) -> DataQualityGate:
    return DataQualityGate(
        DataQualityGateConfig(
            enforcement_mode=EnforcementMode.ENFORCE.value,
            profiles={"crypto_5m": CRYPTO_5M_PROFILE},
            **kw,
        )
    )


def _pair_store(*, source: str) -> MarketStateStore:
    store = MarketStateStore()
    ts = utc_now() - timedelta(milliseconds=100)
    for tid in (YES, NO):
        store.apply_book(
            TokenId(tid),
            [BookLevel(Decimal("0.48"), Decimal("100"))],
            [BookLevel(Decimal("0.52"), Decimal("100"))],
            source=source,
            received_ts=ts,
        )
    return store


def test_entry_blocked_on_rest_bootstrap() -> None:
    gate = _gate()
    pair = _pair_store(source=BookSource.REST_BOOTSTRAP).capture_pair(
        TokenId(YES), TokenId(NO), "p1", now=utc_now()
    )
    report = gate.evaluate_pair(pair, context=DecisionContext.ENTRY, size=Decimal("5"))
    assert report.verdict == QualityVerdict.REJECT_DECISION
    assert not gate.allows_decision(report, DecisionContext.ENTRY)


def test_entry_blocked_on_rest_recovery() -> None:
    gate = _gate()
    pair = _pair_store(source=BookSource.REST_RECOVERY).capture_pair(
        TokenId(YES), TokenId(NO), "p1", now=utc_now()
    )
    report = gate.evaluate_pair(pair, context=DecisionContext.ENTRY, size=Decimal("5"))
    assert report.verdict == QualityVerdict.REJECT_DECISION


def test_entry_blocked_on_rest_poll() -> None:
    gate = _gate()
    pair = _pair_store(source=BookSource.REST_POLL).capture_pair(
        TokenId(YES), TokenId(NO), "p1", now=utc_now()
    )
    report = gate.evaluate_pair(pair, context=DecisionContext.ENTRY, size=Decimal("5"))
    assert report.verdict == QualityVerdict.REJECT_DECISION


def test_stop_rest_recovery_allowed_when_configured() -> None:
    gate = _gate(allow_rest_recovery_for_exit=True)
    pair = _pair_store(source=BookSource.REST_RECOVERY).capture_pair(
        TokenId(YES), TokenId(NO), "p1", now=utc_now()
    )
    report = gate.evaluate_pair(pair, context=DecisionContext.STOP, size=Decimal("5"))
    assert gate.allows_decision(report, DecisionContext.STOP)


def test_rest_recovery_exit_does_not_increase_exposure() -> None:
    """Exit gate allows STOP on REST_RECOVERY; entry/TP remain blocked (no new exposure)."""
    gate = _gate(allow_rest_recovery_for_exit=True)
    store = _pair_store(source=BookSource.REST_RECOVERY)
    pair = store.capture_pair(TokenId(YES), TokenId(NO), "p1", now=utc_now())
    assert not gate.allows_decision(
        gate.evaluate_pair(pair, context=DecisionContext.ENTRY, size=Decimal("5")),
        DecisionContext.ENTRY,
    )
    assert gate.allows_decision(
        gate.evaluate_pair(pair, context=DecisionContext.STOP, size=Decimal("5")),
        DecisionContext.STOP,
    )


def test_ws_primary_entry_blocked_without_readiness() -> None:
    from tyrex_pm.runtime.config import parse_app_config

    app = parse_app_config(
        risk={
            "notional": {"min_usd": "0.01", "max_usd": "1000", "max_policy": "cap"},
            "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
            "venue_min_size": {"enabled": False},
            "capital": {"enabled": False},
        },
        strategy={
            "kind": "paired_binary",
            "enabled": True,
            "paired_binary": {
                "owner_id": "paired_binary",
                "market_id": "m1",
                "yes_token_id": YES,
                "no_token_id": NO,
                "position_size": "5",
                "max_pair_entry_cost": "1.02",
                "max_spread_yes": "0.02",
                "max_spread_no": "0.02",
                "pair_stop_loss_pct": "0.04",
                "pair_take_profit_pct": "0.10",
                "slippage_buffer": "0.005",
            },
        },
        runtime={
            "execution_mode": "shadow",
            "market_data": {
                "enabled": True,
                "websocket": {"primary_enabled": True},
                "quality": {"enforcement_mode": "enforce"},
            },
            "execution": {"planner": {"enabled": True}},
        },
    )
    cfg = app.paired_binary
    assert cfg is not None
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore())
    coord.market_state = _pair_store(source=BookSource.WEBSOCKET)
    from tyrex_pm.market_data.readiness import MarketReadinessTracker

    coord.market_readiness_tracker = MarketReadinessTracker(
        yes_token_id=TokenId(YES), no_token_id=TokenId(NO)
    )
    result = should_block_paired_binary_decision(
        app=app,
        coord=coord,
        cfg=cfg,
        context=DecisionContext.ENTRY,
        size=Decimal("5"),
    )
    assert not result.allowed
    assert result.block_reason == "readiness_not_trading_enabled"
