"""BotSellValidateStrategy — Scenario A harness (no venue)."""

from __future__ import annotations

import pytest
from nautilus_trader.model.identifiers import InstrumentId

from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import OrderIntent
from tyrex_pm.execution.port import NoOpExecutionPort
from tyrex_pm.risk.policy import ShadowAllPassRisk
from tyrex_pm.strategy.bot_sell_validate_strategy import (
    BotSellValidateStrategy,
    BotSellValidateStrategyConfig,
)
from tyrex_pm.strategy.validation_constants import DEFAULT_VALIDATION_SELL_INVENTORY_HAIRCUT_BPS


class DenyAllRisk:
    def evaluate(self, intent: OrderIntent) -> tuple[bool, str, OrderIntent | None]:
        _ = intent
        return False, "test_deny", None


_IID = InstrumentId.from_str("0xabc-88888.POLYMARKET")


def test_submit_validated_sell_clears_pending_when_risk_denies() -> None:
    cfg = BotSellValidateStrategyConfig(
        execution_mode="live",
        sell_delay_seconds=1.0,
        max_cycles=1,
        validation_aggressive_limits=False,
    )
    s = BotSellValidateStrategy(cfg)
    s.set_risk_policy(DenyAllRisk())
    s.set_execution_port(NoOpExecutionPort())
    s._validate_sell_pending = True
    s._submit_validated_sell(
        instrument_id=_IID,
        token_id="tok1",
        quantity_from_buy_fill=2.0,
        price_ref=0.55,
        sell_correlation_id="bot_sell_validate:r1:entry",
        entry_correlation_id="entry",
    )
    assert s._validate_sell_pending is False


def test_submit_validated_sell_submits_when_risk_approves() -> None:
    cfg = BotSellValidateStrategyConfig(
        execution_mode="live",
        sell_delay_seconds=1.0,
        max_cycles=1,
        validation_aggressive_limits=False,
    )
    s = BotSellValidateStrategy(cfg)
    s.set_risk_policy(ShadowAllPassRisk())
    port = NoOpExecutionPort()
    s.set_execution_port(port)
    s._validate_sell_pending = True
    s._submit_validated_sell(
        instrument_id=_IID,
        token_id="tok1",
        quantity_from_buy_fill=2.0,
        price_ref=0.55,
        sell_correlation_id="bot_sell_validate:r1:entry",
        entry_correlation_id="entry",
    )
    assert len(port.records) == 1
    intent, mode = port.records[0]
    assert mode == "live"
    assert intent.side == "SELL"
    assert intent.signal_kind == "exit"
    assert intent.reason_code == str(ReasonCode.BOT_SELL_VALIDATE)
    assert intent.correlation_id == "bot_sell_validate:r1:entry"
    assert s._validate_sell_pending is True


class _VInst:
    size_increment = 0.01
    min_quantity = 0.0


class _VCache:
    def instrument(self, iid: InstrumentId) -> _VInst:
        _ = iid
        return _VInst()


class _VPortfolio:
    def __init__(self, net: float) -> None:
        self._net = net

    def net_position(self, iid: InstrumentId) -> float:
        _ = iid
        return self._net


class _BotSellValidateStrategyForPortfolioTest(BotSellValidateStrategy):
    """Actor ``portfolio`` / ``cache`` are read-only; subclass supplies test doubles."""

    def __init__(
        self,
        cfg: BotSellValidateStrategyConfig,
        portfolio: _VPortfolio,
        cache: _VCache,
    ) -> None:
        object.__setattr__(self, "_vportfolio", portfolio)
        object.__setattr__(self, "_vcache", cache)
        super().__init__(cfg)

    @property
    def portfolio(self) -> _VPortfolio:  # type: ignore[override]
        return self._vportfolio

    @property
    def cache(self) -> _VCache:  # type: ignore[override]
        return self._vcache


def test_submit_validated_sell_uses_portfolio_net_cap_when_wired() -> None:
    cfg = BotSellValidateStrategyConfig(
        execution_mode="live",
        sell_delay_seconds=1.0,
        max_cycles=1,
        validation_aggressive_limits=False,
        validation_sell_inventory_haircut_bps=0.0,
    )
    s = _BotSellValidateStrategyForPortfolioTest(cfg, _VPortfolio(8.489), _VCache())
    s.set_risk_policy(ShadowAllPassRisk())
    port = NoOpExecutionPort()
    s.set_execution_port(port)
    s._validate_sell_pending = True
    s._submit_validated_sell(
        instrument_id=_IID,
        token_id="tok1",
        quantity_from_buy_fill=8.69,
        price_ref=0.55,
        sell_correlation_id="bot_sell_validate:r1:entry",
        entry_correlation_id="entry",
    )
    assert len(port.records) == 1
    intent, _mode = port.records[0]
    assert intent.quantity == pytest.approx(8.48)


def test_submit_validated_sell_applies_default_inventory_haircut_when_wired() -> None:
    cfg = BotSellValidateStrategyConfig(
        execution_mode="live",
        sell_delay_seconds=1.0,
        max_cycles=1,
        validation_aggressive_limits=False,
    )
    assert cfg.validation_sell_inventory_haircut_bps == pytest.approx(
        DEFAULT_VALIDATION_SELL_INVENTORY_HAIRCUT_BPS,
    )
    s = _BotSellValidateStrategyForPortfolioTest(cfg, _VPortfolio(5.26), _VCache())
    s.set_risk_policy(ShadowAllPassRisk())
    port = NoOpExecutionPort()
    s.set_execution_port(port)
    s._validate_sell_pending = True
    s._submit_validated_sell(
        instrument_id=_IID,
        token_id="tok1",
        quantity_from_buy_fill=5.26,
        price_ref=0.55,
        sell_correlation_id="bot_sell_validate:r1:entry",
        entry_correlation_id="entry",
    )
    assert len(port.records) == 1
    intent, _mode = port.records[0]
    assert intent.quantity == pytest.approx(5.15)
    assert intent.quantity < 5.26
