"""CopyStrategy shadow path (no venue orders)."""

from __future__ import annotations

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.portfolio.portfolio import Portfolio

from tyrex_pm.core.types import GuruTradeSignal, OrderIntent
from tyrex_pm.data.guru_monitor import GURU_TRADE_TOPIC
from tyrex_pm.execution.port import NoOpExecutionPort
from tyrex_pm.strategy.copy_strategy import CopyStrategy, CopyStrategyConfig


class DenyAllRisk:
    def evaluate(self, intent: OrderIntent) -> tuple[bool, str]:
        _ = intent
        return False, "test_deny"


def _register(strat: CopyStrategy) -> MessageBus:
    clock = LiveClock()
    cache = Cache(database=None)
    msgbus = MessageBus(trader_id=TraderId("TEST-001"), clock=clock)
    portfolio = Portfolio(msgbus=msgbus, clock=clock, cache=cache)
    strat.register(
        trader_id=TraderId("TEST-001"),
        portfolio=portfolio,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
    )
    return msgbus


def test_shadow_emits_intent_for_filtered_buy() -> None:
    cfg = CopyStrategyConfig(
        token_filter_enabled=True,
        allowlisted_token_ids=("99",),
        execution_mode="shadow",
    )
    strat = CopyStrategy(cfg)
    port = NoOpExecutionPort()
    strat.set_execution_port(port)
    msgbus = _register(strat)
    strat.on_start()

    sig = GuruTradeSignal(
        source_trade_id="trade-1",
        ts_event_ms=1,
        side="BUY",
        token_id="99",
        size_raw=10.0,
        price_raw=0.4,
        raw_payload_ref="m",
    )
    msgbus.publish(GURU_TRADE_TOPIC, sig)

    assert len(port.records) == 1
    intent, mode = port.records[0]
    assert mode == "shadow"
    assert intent.correlation_id == "trade-1"
    assert intent.token_id == "99"
    assert intent.quantity == 10.0
    assert intent.signal_kind == "entry"


def test_not_allowlisted_skips_intent() -> None:
    cfg = CopyStrategyConfig(token_filter_enabled=True, allowlisted_token_ids=("99",))
    strat = CopyStrategy(cfg)
    port = NoOpExecutionPort()
    strat.set_execution_port(port)
    msgbus = _register(strat)
    strat.on_start()

    sig = GuruTradeSignal(
        source_trade_id="trade-2",
        ts_event_ms=1,
        side="BUY",
        token_id="100",
        size_raw=10.0,
        price_raw=0.4,
        raw_payload_ref=None,
    )
    msgbus.publish(GURU_TRADE_TOPIC, sig)
    assert port.records == []


def test_risk_deny_skips_execution() -> None:
    cfg = CopyStrategyConfig(token_filter_enabled=True, allowlisted_token_ids=("99",))
    strat = CopyStrategy(cfg)
    port = NoOpExecutionPort()
    strat.set_execution_port(port)
    strat.set_risk_policy(DenyAllRisk())  # type: ignore[arg-type]
    msgbus = _register(strat)
    strat.on_start()
    sig = GuruTradeSignal(
        source_trade_id="trade-deny",
        ts_event_ms=1,
        side="BUY",
        token_id="99",
        size_raw=5.0,
        price_raw=0.5,
        raw_payload_ref=None,
    )
    msgbus.publish(GURU_TRADE_TOPIC, sig)
    assert port.records == []


def test_sell_mirror_branch() -> None:
    cfg = CopyStrategyConfig(token_filter_enabled=True, allowlisted_token_ids=("99",))
    strat = CopyStrategy(cfg)
    port = NoOpExecutionPort()
    strat.set_execution_port(port)
    msgbus = _register(strat)
    strat.on_start()

    sig = GuruTradeSignal(
        source_trade_id="trade-3",
        ts_event_ms=1,
        side="SELL",
        token_id="99",
        size_raw=3.0,
        price_raw=0.6,
        raw_payload_ref=None,
    )
    msgbus.publish(GURU_TRADE_TOPIC, sig)
    assert len(port.records) == 1
    intent, _ = port.records[0]
    assert intent.signal_kind == "exit"
    assert intent.side == "SELL"


def test_unfiltered_accepts_any_token_buy() -> None:
    cfg = CopyStrategyConfig(
        token_filter_enabled=False,
        allowlisted_token_ids=(),
        execution_mode="shadow",
    )
    strat = CopyStrategy(cfg)
    port = NoOpExecutionPort()
    strat.set_execution_port(port)
    msgbus = _register(strat)
    strat.on_start()

    sig = GuruTradeSignal(
        source_trade_id="trade-open",
        ts_event_ms=1,
        side="BUY",
        token_id="999999999",
        size_raw=2.0,
        price_raw=0.3,
        raw_payload_ref=None,
    )
    msgbus.publish(GURU_TRADE_TOPIC, sig)
    assert len(port.records) == 1
    intent, _ = port.records[0]
    assert intent.token_id == "999999999"
