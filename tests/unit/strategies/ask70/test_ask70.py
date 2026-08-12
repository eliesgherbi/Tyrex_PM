"""ask70 harness strategy and config tests."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.ids import CorrelationId, EventId, InstrumentId, MarketId, RunId, TokenId
from tyrex_pm.core.instruments import Instrument, OutcomeSide
from tyrex_pm.core.intents import EnterIntent
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import ExecutableQuote
from tyrex_pm.market_data.freshness import (
    FreshnessAssessment,
    FreshnessReason,
    TimestampBasis,
)
from tyrex_pm.runtime.run_config import load_trading_run_config
from tyrex_pm.strategies.ask70.config import Ask70Config
from tyrex_pm.strategies.ask70.reasons import Ask70Reason
from tyrex_pm.strategies.ask70.strategy import Ask70Strategy
from tyrex_pm.strategies.context import DecisionContext
from tyrex_pm.strategies.decisions import StrategyAction


def _fresh() -> FreshnessAssessment:
    now = datetime.now(timezone.utc)
    return FreshnessAssessment(
        is_fresh=True,
        age_ms=0,
        threshold_ms=5_000,
        timestamp_basis=TimestampBasis.EVENT_TIME,
        reason_code=FreshnessReason.FRESH,
        observed_at=now,
    )


def _quote(ask: Decimal | None, bid: Decimal | None = None) -> ExecutableQuote:
    return ExecutableQuote(
        best_ask=ask,
        best_bid=bid,
        mid=None,
        spread=None,
        ask_size_at_touch=Decimal("10") if ask is not None else Decimal("0"),
        bid_size_at_touch=Decimal("10") if bid is not None else Decimal("0"),
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
        question="ask70 test",
        yes=yes,
        no=no,
        event_start=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
        event_end=datetime(2026, 8, 11, 12, 5, tzinfo=timezone.utc),
    )


def _snapshot(*, up_ask: Decimal, down_ask: Decimal) -> DecisionSnapshot:
    now = datetime.now(timezone.utc)
    return DecisionSnapshot(
        market=_market(),
        yes_book=None,
        no_book=None,
        yes_quote=_quote(up_ask),
        no_quote=_quote(down_ask),
        reference=None,
        yes_freshness=_fresh(),
        no_freshness=_fresh(),
        reference_freshness=_fresh(),
        observed_at=now,
        correlation_id=CorrelationId("c1"),
        causation_id=EventId("e1"),
    )


def _context(*, entry_allowed: bool = True, position: Decimal = Decimal("0")) -> DecisionContext:
    snap = _snapshot(up_ask=Decimal("0.71"), down_ask=Decimal("0.30"))
    return DecisionContext(
        run_id=RunId("ask70"),
        snapshot=snap,
        target_notional=Decimal("5"),
        now=snap.observed_at,
        entry_allowed=entry_allowed,
        position_quantity=position,
    )


def test_ask70_config_loads_from_yaml() -> None:
    cfg = load_trading_run_config(Path("config/runs/ask70_protection_tiny_live.yaml"))
    assert cfg.strategy_kind == "ask70"
    assert isinstance(cfg.strategy, Ask70Config)
    assert cfg.strategy.entry_ask_threshold == Decimal("0.70")
    assert cfg.protection is not None
    assert cfg.protection.take_profit is not None
    assert cfg.protection.take_profit.absolute == Decimal("0.85")


def test_ask70_enters_first_leg_at_or_above_threshold() -> None:
    strategy = Ask70Strategy(
        config=Ask70Config(
            entry_ask_threshold=Decimal("0.70"),
            leg_preference="first_hit",
            max_price_pad=Decimal("0.02"),
            tau_min_s=10.0,
            tau_max_s=290.0,
            max_clock_uncertainty_ms=750.0,
        )
    )
    snap = _snapshot(up_ask=Decimal("0.71"), down_ask=Decimal("0.72"))
    decision, intents = strategy.on_decision(market_snapshot=snap, context=_context())
    assert decision.action is StrategyAction.ENTER
    assert decision.reason_code == Ask70Reason.ENTRY_ASK_HIT.value
    assert len(intents) == 1
    intent = intents[0]
    assert isinstance(intent, EnterIntent)
    assert intent.outcome is OutcomeSide.YES
    assert intent.max_price == Decimal("0.73")


def test_ask70_waits_when_asks_below_threshold() -> None:
    strategy = Ask70Strategy(
        config=Ask70Config(
            entry_ask_threshold=Decimal("0.70"),
            leg_preference="first_hit",
            max_price_pad=Decimal("0.02"),
            tau_min_s=10.0,
            tau_max_s=290.0,
            max_clock_uncertainty_ms=750.0,
        )
    )
    snap = _snapshot(up_ask=Decimal("0.60"), down_ask=Decimal("0.55"))
    decision, intents = strategy.on_decision(market_snapshot=snap, context=_context())
    assert decision.action is StrategyAction.WAIT
    assert intents == []


def test_ask70_holds_without_exit_intents() -> None:
    strategy = Ask70Strategy(
        config=Ask70Config(
            entry_ask_threshold=Decimal("0.70"),
            leg_preference="first_hit",
            max_price_pad=Decimal("0.02"),
            tau_min_s=10.0,
            tau_max_s=290.0,
            max_clock_uncertainty_ms=750.0,
        )
    )
    snap = _snapshot(up_ask=Decimal("0.71"), down_ask=Decimal("0.30"))
    decision, intents = strategy.on_decision(
        market_snapshot=snap,
        context=_context(position=Decimal("10")),
    )
    assert decision.action is StrategyAction.HOLD
    assert intents == []


def test_z_gap_config_still_loads_without_protection() -> None:
    cfg = load_trading_run_config(Path("config/runs/z_gap_tiny_live.yaml"))
    assert cfg.strategy_kind == "z_gap"
    assert cfg.protection is None


def test_unknown_strategy_kind_is_rejected(tmp_path: Path) -> None:
    from tyrex_pm.runtime.run_config import RunConfigError

    raw = Path("config/runs/z_gap_tiny_live.yaml").read_text(encoding="utf-8")
    tmp = tmp_path / "unknown.yaml"
    tmp.write_text(raw.replace("kind: z_gap", "kind: not_a_strategy"), encoding="utf-8")
    with pytest.raises(RunConfigError, match="not registered"):
        load_trading_run_config(tmp)
