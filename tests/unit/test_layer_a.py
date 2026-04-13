"""Layer A filters and orchestrator (v1)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tyrex_pm.config.loaders import (
    ExitFilterSettings,
    LayerAFiltersSettings,
    SignificanceConvictionSettings,
    SignificanceFilterSettings,
    StaticAmountSettings,
    load_strategy_settings,
)
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.portfolio.portfolio import Portfolio

from tyrex_pm.core.types import GuruTradeSignal
from tyrex_pm.data.guru_monitor import GURU_TRADE_TOPIC
from tyrex_pm.execution.port import NoOpExecutionPort
from tyrex_pm.signal.layer_a import build_layer_a_orchestrator
from tyrex_pm.signal.layer_a.filters.exit_interpretation import ExitInterpretationFilter
from tyrex_pm.signal.layer_a.filters.significance_conviction import SignificanceConvictionFilter
from tyrex_pm.signal.layer_a.filters.static_amount import StaticAmountGatingFilter
from tyrex_pm.signal.layer_a.filters.token_allowlist import TokenAllowlistGatingFilter
from tyrex_pm.signal.layer_a.notional import notional_usd
from tyrex_pm.signal.layer_a.orchestrator import LayerAOrchestrator
from tyrex_pm.signal.layer_a.types import LayerAContext, LayerAOutcome
from tyrex_pm.signal.token_filter_spec import TokenFilterSpec
from tyrex_pm.strategy.copy_strategy import CopyStrategy, CopyStrategyConfig


def _sig(**kwargs) -> GuruTradeSignal:
    base = dict(
        source_trade_id="c1",
        ts_event_ms=1,
        side="BUY",
        token_id="t1",
        size_raw=10.0,
        price_raw=0.5,
        raw_payload_ref=None,
    )
    base.update(kwargs)
    return GuruTradeSignal(**base)


def test_notional_usd() -> None:
    assert notional_usd(_sig()) == 5.0
    assert notional_usd(_sig(price_raw=None)) is None
    assert notional_usd(_sig(price_raw=0.5, size_raw=0)) is None


def test_token_allowlist_entry_exit() -> None:
    tok = TokenFilterSpec(enabled=True, allowlisted=frozenset({"t1"}))
    f = TokenAllowlistGatingFilter(tok)
    o = f.evaluate(_sig(), branch="entry")
    assert o.accept
    o2 = f.evaluate(_sig(token_id="x"), branch="entry")
    assert not o2.accept
    o3 = f.evaluate(_sig(side="SELL"), branch="entry")
    assert not o3.accept
    o4 = f.evaluate(_sig(side="SELL"), branch="exit")
    assert o4.accept


def test_static_amount_matrix() -> None:
    st = StaticAmountSettings(True, 700.0)
    f = StaticAmountGatingFilter(st)
    assert f.evaluate(_sig(price_raw=0.5, size_raw=2000), branch="entry").accept  # 1000 >= 700
    assert not f.evaluate(_sig(price_raw=0.5, size_raw=1000), branch="entry").accept  # 500 < 700
    assert f.evaluate(_sig(price_raw=0.7, size_raw=1000), branch="entry").accept  # boundary ==
    assert not f.evaluate(_sig(price_raw=None), branch="entry").accept
    assert not f.evaluate(_sig(size_raw=None), branch="entry").accept
    assert f.evaluate(_sig(), branch="exit").accept


def test_significance_cold_start_and_median() -> None:
    cv = SignificanceConvictionSettings(True, 20, "median")
    f = SignificanceConvictionFilter(cv)
    assert f.evaluate(_sig(price_raw=1.0, size_raw=100), branch="entry").accept
    f.observe_buy(_sig(price_raw=1.0, size_raw=100), token_gating_passed=True)
    # prior [100], current 50, median 100, deny
    assert not f.evaluate(_sig(price_raw=0.5, size_raw=100), branch="entry").accept
    # equality deny: current 100, median 100
    assert not f.evaluate(_sig(price_raw=1.0, size_raw=100), branch="entry").accept
    # strictly above: 100.01 >100
    assert f.evaluate(_sig(price_raw=1.0001, size_raw=100), branch="entry").accept


def test_significance_even_length_median() -> None:
    cv = SignificanceConvictionSettings(True, 10, "median")
    f = SignificanceConvictionFilter(cv)
    for px, sz in [(1.0, 10), (1.0, 30), (1.0, 40), (1.0, 50)]:
        f.observe_buy(_sig(price_raw=px, size_raw=float(sz)), token_gating_passed=True)
    # prior [10,30,40,50] median (30+40)/2=35; current 36 > 35
    assert f.evaluate(_sig(price_raw=1.0, size_raw=36), branch="entry").accept
    assert not f.evaluate(_sig(price_raw=1.0, size_raw=35), branch="entry").accept


def test_significance_static_deny_still_observe() -> None:
    tok = TokenAllowlistGatingFilter(TokenFilterSpec(True, frozenset({"t1"})))
    st = StaticAmountGatingFilter(StaticAmountSettings(True, 10_000.0))
    cv = SignificanceConvictionSettings(True, 5, "median")
    sigf = SignificanceConvictionFilter(cv)
    ex = ExitInterpretationFilter(ExitFilterSettings(False, "mirror_guru"))
    orch = LayerAOrchestrator(
        token=tok,
        static=st,
        significance=sigf,
        exit_interpretation=ex,
    )
    s_small = _sig(price_raw=0.5, size_raw=10)  # notional 5 < 10000
    out, _recs = orch.run(s_small, branch="entry", ctx=None)
    assert not out.accept
    # cold start would pass static fail first — after deny, one BUY in deque from observe
    assert len(sigf._buf) == 1  # noqa: SLF001 — test inspects deque


def test_significance_token_deny_no_observe() -> None:
    tok = TokenAllowlistGatingFilter(TokenFilterSpec(True, frozenset({"t1"})))
    st = StaticAmountGatingFilter(StaticAmountSettings(False, 0.0))
    cv = SignificanceConvictionSettings(True, 5, "median")
    sigf = SignificanceConvictionFilter(cv)
    ex = ExitInterpretationFilter(ExitFilterSettings(False, "mirror_guru"))
    orch = LayerAOrchestrator(token=tok, static=st, significance=sigf, exit_interpretation=ex)
    out, _ = orch.run(_sig(token_id="bad"), branch="entry", ctx=None)
    assert not out.accept
    assert len(sigf._buf) == 0  # noqa: SLF001


def test_significance_missing_notional_no_append() -> None:
    cv = SignificanceConvictionSettings(True, 5, "median")
    f = SignificanceConvictionFilter(cv)
    assert not f.evaluate(_sig(price_raw=None, size_raw=10), branch="entry").accept
    f.observe_buy(_sig(price_raw=None, size_raw=10), token_gating_passed=True)
    assert len(f._buf) == 0  # noqa: SLF001


class _Ctx(LayerAContext):
    def __init__(self, v: float | None, *, raise_exc: bool = False) -> None:
        self._v = v
        self._raise = raise_exc

    def follower_long_qty_for_outcome_token(self, token_id: str) -> float | None:
        _ = token_id
        if self._raise:
            msg = "boom"
            raise RuntimeError(msg)
        return self._v


def test_exit_full_exit_matrix() -> None:
    exs = ExitFilterSettings(True, "full_exit")
    f = ExitInterpretationFilter(exs)
    assert f.evaluate(_sig(side="SELL"), branch="exit", ctx=_Ctx(10.0)).accept
    assert not f.evaluate(_sig(side="SELL"), branch="exit", ctx=_Ctx(None)).accept
    assert not f.evaluate(_sig(side="SELL"), branch="exit", ctx=_Ctx(0.0)).accept
    assert not f.evaluate(_sig(side="SELL"), branch="exit", ctx=None).accept
    assert not f.evaluate(_sig(side="SELL", token_id=None), branch="exit", ctx=_Ctx(1.0)).accept
    assert not f.evaluate(_sig(side="SELL"), branch="exit", ctx=_Ctx(1.0, raise_exc=True)).accept


def test_exit_mirror_when_disabled() -> None:
    f = ExitInterpretationFilter(ExitFilterSettings(False, "mirror_guru"))
    o = f.evaluate(_sig(side="SELL"), branch="exit", ctx=None)
    assert o.accept
    assert o.metadata.get("exit_qty_mode") == "mirror_guru"


def test_loader_filters_rejects(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "guru_wallet_address": "0x1234567890123456789012345678901234567890",
                "token_filter": {"enabled": False, "allowlisted_token_ids": []},
                "filters": {
                    "exit_filter": {"enabled": True, "exit_method": "bogus"},
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exit_method"):
        load_strategy_settings(p)

    p2 = tmp_path / "s2.yaml"
    p2.write_text(
        yaml.safe_dump(
            {
                "guru_wallet_address": "0x1234567890123456789012345678901234567890",
                "token_filter": {"enabled": False, "allowlisted_token_ids": []},
                "filters": {
                    "significance_filter": {
                        "significance_conviction": {
                            "enabled": True,
                            "lookback_trades": 5,
                            "threshold_method": "mean",
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="median"):
        load_strategy_settings(p2)


def test_orchestrator_exit_ordering() -> None:
    tok = TokenAllowlistGatingFilter(TokenFilterSpec(True, frozenset({"t1"})))
    orch = build_layer_a_orchestrator(
        LayerAFiltersSettings(
            exit_filter=ExitFilterSettings(False, "mirror_guru"),
            significance_filter=SignificanceFilterSettings(
                StaticAmountSettings(False, 0.0),
                SignificanceConvictionSettings(False, 20, "median"),
            ),
        ),
        TokenFilterSpec(True, frozenset({"t1"})),
    )
    out, recs = orch.run(_sig(side="SELL"), branch="exit", ctx=None)
    assert out.accept
    assert recs[0].filter_name == "token_allowlist"
    assert recs[1].filter_name == "exit_interpretation"


def test_layer_a_outcome_json_metadata() -> None:
    o = LayerAOutcome(True, "x", None, {"a": 1, "b": {"c": 2}})
    assert o.metadata["a"] == 1
    assert o.metadata["b"]["c"] == 2


def _register_copy(strat: CopyStrategy) -> MessageBus:
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


def test_copy_strategy_full_exit_uses_context_qty() -> None:
    la = LayerAFiltersSettings(
        exit_filter=ExitFilterSettings(True, "full_exit"),
        significance_filter=SignificanceFilterSettings(
            StaticAmountSettings(False, 0.0),
            SignificanceConvictionSettings(False, 20, "median"),
        ),
    )
    cfg = CopyStrategyConfig(
        token_filter_enabled=True,
        allowlisted_token_ids=("t1",),
        execution_mode="shadow",
        copy_scale=0.5,
        layer_a=la,
    )
    strat = CopyStrategy(cfg)
    port = NoOpExecutionPort()
    strat.set_execution_port(port)
    strat.set_layer_a_context(_Ctx(42.0))
    msgbus = _register_copy(strat)
    strat.on_start()
    msgbus.publish(
        GURU_TRADE_TOPIC,
        _sig(side="SELL", token_id="t1", size_raw=1.0, price_raw=0.9),
    )
    assert len(port.records) == 1
    intent, _mode = port.records[0]
    assert intent.quantity == 42.0
    assert intent.signal_kind == "exit"


def test_copy_strategy_layer_a_deny_no_submit() -> None:
    la = LayerAFiltersSettings(
        exit_filter=ExitFilterSettings(False, "mirror_guru"),
        significance_filter=SignificanceFilterSettings(
            StaticAmountSettings(True, 1_000_000.0),
            SignificanceConvictionSettings(False, 20, "median"),
        ),
    )
    cfg = CopyStrategyConfig(
        token_filter_enabled=True,
        allowlisted_token_ids=("t1",),
        execution_mode="shadow",
        layer_a=la,
    )
    strat = CopyStrategy(cfg)
    port = NoOpExecutionPort()
    strat.set_execution_port(port)
    msgbus = _register_copy(strat)
    strat.on_start()
    msgbus.publish(GURU_TRADE_TOPIC, _sig(token_id="t1", size_raw=1.0, price_raw=0.5))
    assert port.records == []
