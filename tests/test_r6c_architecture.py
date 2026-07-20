"""R6C one-host architecture invariants + LIVE_TINY still disabled."""

from __future__ import annotations

import ast
from pathlib import Path

import tyrex_pm
from tyrex_pm.runtime.observe_host import ObserveHost, TradingHost
from tyrex_pm.runtime.shadow_host import ShadowHost

PKG = Path(tyrex_pm.__file__).resolve().parent


def test_one_host_signal_intent_facts_startup_shared() -> None:
    assert TradingHost is ObserveHost
    assert issubclass(ShadowHost, ObserveHost)
    observe = (PKG / "runtime" / "observe_host.py").read_text(encoding="utf-8")
    shadow = (PKG / "runtime" / "shadow_host.py").read_text(encoding="utf-8")
    assert "def evaluate_once" in observe
    assert "def evaluate_once" not in shadow
    binding = (PKG / "runtime" / "strategy_binding.py").read_text(encoding="utf-8")
    # Signal construction lives in the strategy binding (F4), not duplicated in hosts.
    assert "build_directional_signal" in binding
    assert "build_directional_signal" not in observe
    assert "build_directional_signal" not in shadow
    assert "def run_fixture" in observe
    assert "_handle_shadow_transition" in shadow or "self.oms" in shadow
    # Paper/shadow rename deferred — compatibility note only
    assert "TradingHost = ObserveHost" in observe


def test_only_execution_dispatch_differs() -> None:
    shadow = (PKG / "runtime" / "shadow_host.py").read_text(encoding="utf-8")
    assert "ShadowOMS" in shadow or "self.oms" in shadow
    assert "def _process_transition" in shadow
    assert "def _build_decision_context" in shadow


def test_no_live_host_class() -> None:
    for path in (PKG / "runtime").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "class LiveHost" not in text, path.name


def test_no_old_or_nautilus() -> None:
    for path in PKG.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(("old", "nautilus"))
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("old", "nautilus"))


def test_live_tiny_dispatch_still_disabled() -> None:
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    from tyrex_pm.core.ids import MarketId, StrategyId, TokenId, new_correlation_id
    from tyrex_pm.core.intents import EnterIntent, new_intent_id
    from tyrex_pm.core.modes import RuntimeMode
    from tyrex_pm.domain.polymarket.market import BinaryMarket, MarketStatus, make_binary_instruments
    from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
    from tyrex_pm.market_data.executable import ExecutableQuote
    from tyrex_pm.market_data.freshness import FreshnessAssessment, FreshnessReason, TimestampBasis
    from tyrex_pm.risk.context import BookReadiness, RiskConfigView, RiskContext
    from tyrex_pm.risk.dedup import IntentDedupRegistry
    from tyrex_pm.risk.engine import RiskEngine
    from tyrex_pm.risk.reasons import RiskReason

    ts = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    mid = MarketId("m1")
    yes, no = make_binary_instruments(market_id=mid, yes_token=TokenId("y"), no_token=TokenId("n"))
    market = BinaryMarket(
        market_id=mid, condition_id="m1", question="q", yes=yes, no=no, status=MarketStatus.ACTIVE
    )
    fresh = FreshnessAssessment(
        is_fresh=True,
        age_ms=1,
        threshold_ms=1000,
        timestamp_basis=TimestampBasis.EVENT_TIME,
        reason_code=FreshnessReason.FRESH,
        observed_at=ts,
    )
    q = ExecutableQuote(
        best_bid=Decimal("0.48"),
        best_ask=Decimal("0.52"),
        mid=Decimal("0.5"),
        spread=Decimal("0.04"),
        bid_size_at_touch=Decimal("100"),
        ask_size_at_touch=Decimal("100"),
    )
    snap = DecisionSnapshot(
        market=market,
        yes_book=None,
        no_book=None,
        yes_quote=q,
        no_quote=q,
        reference=None,
        yes_freshness=fresh,
        no_freshness=fresh,
        reference_freshness=fresh,
        observed_at=ts,
        correlation_id=new_correlation_id(),
    )
    ctx = RiskContext(
        mode=RuntimeMode.LIVE_TINY,
        now=ts,
        market=market,
        snapshot=snap,
        yes_quote=q,
        no_quote=q,
        yes_book=BookReadiness(initialized=True, recovery_required=False, tick_size=Decimal("0.01")),
        no_book=BookReadiness(initialized=True, recovery_required=False, tick_size=Decimal("0.01")),
        risk_config=RiskConfigView(
            max_notional=Decimal("10"),
            min_price=Decimal("0.01"),
            max_price=Decimal("0.99"),
            max_spread=Decimal("0.2"),
            min_liquidity_notional=Decimal("1"),
            no_entry_before_close=timedelta(0),
            kill_switch_active=False,
            config_fingerprint="fp",
        ),
        dedup=IntentDedupRegistry(lifetime=timedelta(hours=1)),
        exposure_available=False,
    )
    intent = EnterIntent(
        intent_id=new_intent_id(),
        strategy_id=StrategyId("reference_momentum"),
        instrument_id=yes.instrument_id,
        market_id=mid,
        created_at=ts,
        correlation_id=new_correlation_id(),
        causation_id=None,
        reason_code="TEST",
        target_notional=Decimal("5"),
    )
    d = RiskEngine().evaluate(intent, ctx)
    assert not d.approved
    assert RiskReason.LIVE_NOT_SUPPORTED in d.reason_codes
